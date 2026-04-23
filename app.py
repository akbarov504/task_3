import can
import time
import threading
import re
import subprocess
import sys
import serial
import pynmea2
import os
import json
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)

# =========================
# COMMON
# =========================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================
# CAN CONFIG
# =========================
KM_TO_MILES = 0.621371
OFF_TIMEOUT_SECONDS = 3
ENGINE_HOURS_REQUEST_INTERVAL = 5.0
VIN_REQUEST_INTERVAL = 30.0

PGN_REQUEST = 0x00EA00
PGN_VIN = 0x00FEEC
PGN_ENGINE_HOURS = 0x00FEE5

CHANNEL = "can0"
INTERFACE = "socketcan"
BITRATE = 250000
DBITRATE = 250000
USE_FD = False   # FD bo'lsa True qilasan

can_data = {
    "vehicle_speed": 0.0,        # mph
    "engine_speed": None,        # rpm
    "wheel_based_speed": 0.0,    # mph
    "fuel_level": 0.0,           # %
    "trip_distance": 0.0,        # miles
    "total_distance": 0.0,       # miles
    "engine_load": None,         # %
    "engine_temp": None,         # C
    "vin": "",
    "def_level": 0.0,            # %
    "engine_hours": 0.0,         # hours
    "status": "OFF",
    "timestamp": None
}

last_msg_time = 0.0
was_off = True

trip_start_total_distance_miles = None
fuel_tank_1 = None
fuel_tank_2 = None

vin_buffer = bytearray()
vin_expected_size = 0
vin_done = False
vin_tp_active = False

last_engine_hours_request_time = 0.0
last_vin_request_time = 0.0


def ensure_can_interface():
    """can0 up ekanini tekshiradi, down bo'lsa avtomatik up qiladi."""
    try:
        result = subprocess.run(
            ["ip", "link", "show", CHANNEL],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"[ERROR] {CHANNEL} topilmadi.")
            sys.exit(1)

        if "state UP" in result.stdout:
            print(f"[INFO] {CHANNEL} already UP.")
            return

        print(f"[INFO] {CHANNEL} down. Bringing it up...")

        subprocess.run(["sudo", "ip", "link", "set", CHANNEL, "down"], check=False)

        if USE_FD:
            subprocess.run([
                "sudo", "ip", "link", "set", CHANNEL, "type", "can",
                "bitrate", str(BITRATE),
                "dbitrate", str(DBITRATE),
                "fd", "on"
            ], check=True)
        else:
            subprocess.run([
                "sudo", "ip", "link", "set", CHANNEL, "type", "can",
                "bitrate", str(BITRATE)
            ], check=True)

        subprocess.run(["sudo", "ip", "link", "set", CHANNEL, "up"], check=True)

        time.sleep(0.5)
        print(f"[SUCCESS] {CHANNEL} is now UP.")

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Could not configure {CHANNEL}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] Unexpected error while configuring {CHANNEL}: {e}")
        sys.exit(1)


def extract_pgn(arbitration_id: int) -> int:
    pf = (arbitration_id >> 16) & 0xFF
    ps = (arbitration_id >> 8) & 0xFF

    if pf < 240:
        return pf << 8
    return (pf << 8) | ps


def kmh_to_mph(kmh: float) -> float:
    return kmh * KM_TO_MILES


def km_to_miles(km: float) -> float:
    return km * KM_TO_MILES


def sanitize_vin(raw_bytes: bytes) -> str:
    text = raw_bytes.decode("ascii", errors="ignore")
    text = re.sub(r"[^A-Za-z0-9]", "", text).upper()

    matches = re.findall(r"[A-HJ-NPR-Z0-9]{17}", text)
    if matches:
        return matches[0]

    return text[:17]


def reset_can_runtime() -> None:
    global fuel_tank_1, fuel_tank_2, trip_start_total_distance_miles
    global vin_buffer, vin_expected_size, vin_done, vin_tp_active

    can_data["vehicle_speed"] = 0.0
    can_data["engine_speed"] = None
    can_data["wheel_based_speed"] = 0.0
    can_data["engine_load"] = None
    can_data["engine_temp"] = None
    can_data["fuel_level"] = 0.0
    can_data["trip_distance"] = 0.0
    can_data["status"] = "OFF"
    can_data["timestamp"] = now_iso()

    fuel_tank_1 = None
    fuel_tank_2 = None
    trip_start_total_distance_miles = None

    vin_buffer = bytearray()
    vin_expected_size = 0
    vin_done = False
    vin_tp_active = False


def decode_rpm(d: bytes):
    if len(d) < 5:
        return None
    raw = d[3] | (d[4] << 8)
    return None if raw >= 0xFFFA else raw * 0.125


def decode_engine_load(d: bytes):
    if len(d) < 3:
        return None
    return None if d[2] == 0xFF else float(d[2])


def decode_speed_kmh(d: bytes):
    if len(d) < 3:
        return None
    raw = d[1] | (d[2] << 8)
    return None if raw == 0xFFFF else raw * 0.00390625


def decode_temp(d: bytes):
    if len(d) < 1:
        return None
    return None if d[0] == 0xFF else d[0] - 40


def decode_fuel(d: bytes):
    if len(d) < 2:
        return None
    return None if d[1] == 0xFF else d[1] * 0.4


def decode_distance_km(d: bytes):
    if len(d) < 4:
        return None
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    return None if raw == 0xFFFFFFFF else raw * 0.125


def decode_engine_hours(d: bytes):
    if len(d) < 4:
        return None
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    return None if raw == 0xFFFFFFFF else raw * 0.05


def decode_def(d: bytes):
    if len(d) < 1:
        return None
    return None if d[0] == 0xFF else d[0] * 0.4


def request_pgn(bus, requested_pgn: int, label: str) -> None:
    pgn_bytes = [
        requested_pgn & 0xFF,
        (requested_pgn >> 8) & 0xFF,
        (requested_pgn >> 16) & 0xFF,
    ]

    msg = can.Message(
        arbitration_id=0x18EAFF00,
        data=pgn_bytes + [0xFF] * 5,
        is_extended_id=True
    )
    bus.send(msg)
    print(f"{label} requested")


def request_vin(bus) -> None:
    request_pgn(bus, PGN_VIN, "VIN")


def request_engine_hours(bus) -> None:
    request_pgn(bus, PGN_ENGINE_HOURS, "Engine Hours")


def start_vin_tp_if_matches(msg_data: bytes) -> bool:
    global vin_expected_size, vin_buffer, vin_done, vin_tp_active

    if len(msg_data) < 8:
        return False

    control_byte = msg_data[0]
    if control_byte != 32:
        return False

    target_pgn = msg_data[5] | (msg_data[6] << 8) | (msg_data[7] << 16)
    if target_pgn != PGN_VIN:
        return False

    vin_expected_size = msg_data[1] | (msg_data[2] << 8)
    vin_buffer = bytearray()
    vin_done = False
    vin_tp_active = True
    return True


def consume_vin_tp_data(msg_data: bytes) -> None:
    global vin_buffer, vin_done, vin_tp_active

    if not vin_tp_active or vin_done:
        return

    if len(msg_data) < 2:
        return

    vin_buffer.extend(msg_data[1:])

    if len(vin_buffer) >= vin_expected_size:
        raw_vin = bytes(vin_buffer[:vin_expected_size]).rstrip(b"\x00\xff ")
        cleaned = sanitize_vin(raw_vin)
        if cleaned:
            can_data["vin"] = cleaned
        vin_done = True
        vin_tp_active = False


def create_bus():
    ensure_can_interface()

    if USE_FD:
        return can.interface.Bus(
            channel=CHANNEL,
            interface=INTERFACE,
            bitrate=BITRATE,
            fd=True
        )
    else:
        return can.interface.Bus(
            channel=CHANNEL,
            interface=INTERFACE
        )


def can_reader() -> None:
    global last_msg_time, was_off, trip_start_total_distance_miles
    global fuel_tank_1, fuel_tank_2
    global last_engine_hours_request_time, last_vin_request_time

    while True:
        bus = None
        try:
            bus = create_bus()

            request_vin(bus)
            request_engine_hours(bus)
            now = time.time()
            last_engine_hours_request_time = now
            last_vin_request_time = now

            print("CAN thread started")

            while True:
                msg = bus.recv(timeout=1)
                now = time.time()

                if now - last_engine_hours_request_time >= ENGINE_HOURS_REQUEST_INTERVAL:
                    try:
                        request_engine_hours(bus)
                    except Exception as e:
                        print(f"Engine Hours request error: {e}")
                    last_engine_hours_request_time = now

                if now - last_vin_request_time >= VIN_REQUEST_INTERVAL and not can_data["vin"]:
                    try:
                        request_vin(bus)
                    except Exception as e:
                        print(f"VIN request error: {e}")
                    last_vin_request_time = now

                if msg:
                    last_msg_time = now
                    can_data["timestamp"] = now_iso()
                elif now - last_msg_time > OFF_TIMEOUT_SECONDS:
                    if can_data["status"] != "OFF":
                        reset_can_runtime()
                        was_off = True
                    continue

                if not msg or not msg.is_extended_id:
                    continue

                if was_off:
                    can_data["status"] = "ON"
                    was_off = False
                    trip_start_total_distance_miles = None
                else:
                    can_data["status"] = "ON"

                pgn = extract_pgn(msg.arbitration_id)

                if pgn == 61444:
                    can_data["engine_speed"] = decode_rpm(msg.data)
                    can_data["engine_load"] = decode_engine_load(msg.data)

                elif pgn == 65265:
                    speed_kmh = decode_speed_kmh(msg.data)
                    if speed_kmh is not None:
                        speed_mph = kmh_to_mph(speed_kmh)
                        can_data["vehicle_speed"] = speed_mph
                        can_data["wheel_based_speed"] = speed_mph

                elif pgn == 65262:
                    temp = decode_temp(msg.data)
                    if temp is not None:
                        can_data["engine_temp"] = temp

                elif pgn == 65276:
                    f = decode_fuel(msg.data)
                    if f is not None:
                        fuel_tank_1 = f

                elif pgn == 65277:
                    f = decode_fuel(msg.data)
                    if f is not None:
                        fuel_tank_2 = f

                elif pgn == 65248:
                    dist_km = decode_distance_km(msg.data)
                    if dist_km is not None:
                        dist_miles = km_to_miles(dist_km)
                        can_data["total_distance"] = round(dist_miles, 2)

                        if trip_start_total_distance_miles is None:
                            trip_start_total_distance_miles = dist_miles

                        if dist_miles >= trip_start_total_distance_miles:
                            can_data["trip_distance"] = round(dist_miles - trip_start_total_distance_miles, 2)

                elif pgn == 65253:
                    h = decode_engine_hours(msg.data)
                    if h is not None:
                        can_data["engine_hours"] = h

                elif pgn == 65110:
                    d = decode_def(msg.data)
                    if d is not None:
                        can_data["def_level"] = d

                elif pgn == 0xEC00:
                    start_vin_tp_if_matches(msg.data)

                elif pgn == 0xEB00:
                    consume_vin_tp_data(msg.data)

                if fuel_tank_1 is not None and fuel_tank_2 is not None:
                    can_data["fuel_level"] = round((fuel_tank_1 + fuel_tank_2) / 2, 2)
                elif fuel_tank_1 is not None:
                    can_data["fuel_level"] = round(fuel_tank_1, 2)

        except Exception as e:
            print(f"[ERROR] CAN reader error: {e}")
            can_data["status"] = "OFF"
            time.sleep(2)

        finally:
            try:
                if bus is not None:
                    bus.shutdown()
            except Exception:
                pass


# =========================
# GPS CONFIG
# =========================
PORT = "/dev/ttyUSB1"
BAUD = 115200

# Shared data (will be updated by background thread)
_gps_data = {
    "lat": 0.0,
    "lon": 0.0,
    "speed_mph": 0.0,
    "direction": "NW",
    "degree": 0.0,
    "state": "N/A",
    "state_code": "N/A"
}

# --- CONFIGURATION (Matching gps_parse.py) ---
# Primary absolute path
GEOJSON_DIR_ABS = "/usr/local/share/geoJSON"
# Fallback relative path
GEOJSON_DIR_REL = "./geoJSON"

_state_boundaries = []

def _is_point_in_poly(x, y, poly):
    """
    Ray-casting algorithm to check if point (x,y) is inside polygon.
    x: longitude, y: latitude
    poly: list of [lon, lat] points
    """
    n = len(poly)
    inside = False
    p1x, p1y = poly[0]
    for i in range(n + 1):
        p2x, p2y = poly[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside

def _load_boundaries():
    """Loads GeoJSON files from directory and calculates bounding boxes for speed optimization."""
    global _state_boundaries
    
    # Determine which directory to use
    target_dir = GEOJSON_DIR_ABS
    if not os.path.exists(target_dir):
        if os.path.exists(GEOJSON_DIR_REL):
            target_dir = GEOJSON_DIR_REL
        else:
            print(f"[Error] GeoJSON directory not found at {GEOJSON_DIR_ABS} or {GEOJSON_DIR_REL}")
            return

    print(f"Loading state boundaries from {target_dir}...")
    count = 0
    
    try:
        files = sorted(os.listdir(target_dir))
    except OSError:
        print(f"[Error] Accessing directory {target_dir}")
        return

    for fname in files:
        if fname.endswith(".geojson"):
            try:
                with open(os.path.join(target_dir, fname), "r") as f:
                    data = json.load(f)
                    
                    if data.get("type") == "FeatureCollection":
                         features = data.get("features", [])
                         if features: data = features[0]

                    props = data.get("properties", {})
                    name = props.get("name", "Unknown")
                    abbr = props.get("abbreviation", "UNK")
                    geometry = data.get("geometry", {})
                    
                    min_x, min_y, max_x, max_y = 180.0, 90.0, -180.0, -90.0
                    
                    def update_bbox(ring):
                        nonlocal min_x, min_y, max_x, max_y
                        for p in ring:
                            px, py = p
                            if px < min_x: min_x = px
                            if px > max_x: max_x = px
                            if py < min_y: min_y = py
                            if py > max_y: max_y = py
                            
                    gtype = geometry.get("type")
                    coords = geometry.get("coordinates")
                    
                    valid = False
                    if gtype == "Polygon":
                        for ring in coords: update_bbox(ring)
                        valid = True
                    elif gtype == "MultiPolygon":
                        for poly in coords:
                            for ring in poly: update_bbox(ring)
                        valid = True
                        
                    if valid:
                        _state_boundaries.append({
                            "name": name,
                            "abbr": abbr,
                            "geometry": geometry,
                            "bbox": (min_x, min_y, max_x, max_y)
                        })
                        count += 1
            except Exception as e:
                print(f"Error loading {fname}: {e}")
    print(f"Loaded {count} state boundaries.")

def get_state_and_code(lat, lon):
    """Finds state by checking if point is inside loaded polygons."""
    if not _state_boundaries:
        return "N/A", "N/A"
        
    for state in _state_boundaries:
        # 1. Fast Bounding Box Check
        min_x, min_y, max_x, max_y = state["bbox"]
        if not (min_x <= lon <= max_x and min_y <= lat <= max_y):
            continue
            
        # 2. Precise Polygon Check
        geom = state["geometry"]
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        
        found = False
        if gtype == "Polygon":
            if _is_point_in_poly(lon, lat, coords[0]):
                found = True
        elif gtype == "MultiPolygon":
            for poly in coords:
                if _is_point_in_poly(lon, lat, poly[0]):
                    found = True
                    break
        
        if found:
            return state["name"], state["abbr"]

    return "N/A", "N/A"

def _degrees_to_direction(deg):
    if deg is None:
        return "NW"
    deg = float(deg)
    if (deg >= 337.5) or (deg < 22.5):
        return "N"
    elif deg < 67.5:
        return "NE"
    elif deg < 112.5:
        return "E"
    elif deg < 157.5:
        return "SE"
    elif deg < 202.5:
        return "S"
    elif deg < 247.5:
        return "SW"
    elif deg < 292.5:
        return "W"
    else:
        return "NW"

def _open_serial():
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=1)
            return ser
        except Exception as e:
            print(f"Error opening serial port: {e}")
            time.sleep(2)

def gps_reader():
    ser = _open_serial()
    
    _load_boundaries()
    while True:
        try:
            line = ser.readline().decode('ascii', errors='replace').strip()
            if line.startswith('$GPRMC'):
                try:
                    msg = pynmea2.parse(line)

                    # Latitude / Longitude
                    _gps_data["lat"] = msg.latitude
                    _gps_data["lon"] = msg.longitude

                    # Speed
                    speed_knots = msg.spd_over_grnd
                    if speed_knots is None or speed_knots == "":
                        speed_knots = 0.0
                    else:
                        speed_knots = float(speed_knots)
                    _gps_data["speed_mph"] = speed_knots * 1.15078

                    # Direction
                    _gps_data["direction"] = _degrees_to_direction(msg.true_course)
                    _gps_data["degree"] = msg.true_course
                    if not _state_boundaries:
                        print("No boundaries loaded. Exiting.")
                        _gps_data["state"], _gps_data["state_code"] = "N/A", "N/A"
                    else:
                        _gps_data["state"], _gps_data["state_code"] = get_state_and_code(msg.latitude, msg.longitude)
                except pynmea2.ParseError:
                    pass
        except Exception as e:
            print(f"Error reading GPS data: {e}")
            ser.close()
            ser = _open_serial()

# =========================
# SINGLE API
# =========================
@app.route("/api/telemetry", methods=["GET"])
def get_telemetry():
    return jsonify({
        "can": can_data,
        "gps": _gps_data
    })


if __name__ == "__main__":
    can_thread = threading.Thread(target=can_reader, daemon=True)
    gps_thread = threading.Thread(target=gps_reader, daemon=True)

    can_thread.start()
    gps_thread.start()

    app.run(host="0.0.0.0", port=8080)
