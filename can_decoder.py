import re
import sys
import can
import time
import subprocess
from datetime import datetime, timezone

KM_TO_MILES = 0.621371
OFF_TIMEOUT_SECONDS = 3
ENGINE_HOURS_REQUEST_INTERVAL = 5.0
VIN_REQUEST_INTERVAL = 30.0

PGN_VIN = 0x00FEEC
PGN_ENGINE_HOURS = 0x00FEE5

CHANNEL = "can0"
INTERFACE = "socketcan"
BITRATE = 250000
DBITRATE = 250000
USE_FD = False

AUTO_DETECT_BITRATE = True
CANDIDATE_BITRATES = [250000, 500000]
BITRATE_DETECT_TIMEOUT = 2.0
BITRATE_MIN_VALID_FRAMES = 3

can_data = {
    "vehicle_speed": 0.0,
    "engine_speed": None,
    "wheel_based_speed": 0.0,
    "fuel_level": 0.0,
    "trip_distance": 0.0,
    "total_distance": 0.0,
    "engine_load": None,
    "engine_temp": None,
    "vin": "",
    "def_level": 0.0,
    "engine_hours": 0.0,
    "status": "OFF",
    "timestamp": None,
    "check_engine_status": "OFF",
    "active_errors": [],

    "turn_signal_left": "OFF",
    "turn_signal_right": "OFF",
    "seat_belt": "N/A",
    "steering_angle_rad": None,
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

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _interface_is_up(channel: str) -> bool:
    result = subprocess.run(
        ["ip", "link", "show", channel],
        capture_output=True,
        text=True
    )
    return result.returncode == 0 and "state UP" in result.stdout

def _set_link_up_with_retry(channel: str, attempts: int = 3, delay: float = 0.4) -> bool:
    last_err = ""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(
            ["sudo", "ip", "link", "set", channel, "up"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            return True
        last_err = result.stderr.strip()
        print(f"[WARN] {channel} up ({attempt}/{attempts}-urinish) muvaffaqiyatsiz: {last_err}")
        time.sleep(delay)
    print(f"[ERROR] {channel} ni up qilib bo'lmadi: {last_err}")
    return False

def _configure_can_bitrate(channel: str, bitrate: int, dbitrate: int = None, use_fd: bool = False) -> bool:
    try:
        subprocess.run(["sudo", "ip", "link", "set", channel, "down"], check=False)
        time.sleep(0.2)

        if use_fd:
            subprocess.run([
                "sudo", "ip", "link", "set", channel, "type", "can",
                "bitrate", str(bitrate),
                "dbitrate", str(dbitrate or bitrate),
                "fd", "on"
            ], check=True)
        else:
            subprocess.run([
                "sudo", "ip", "link", "set", channel, "type", "can",
                "bitrate", str(bitrate)
            ], check=True)

        if not _set_link_up_with_retry(channel):
            return False

        time.sleep(0.3)
        return True

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {channel} ni {bitrate} bitrate bilan sozlab bo'lmadi: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] {channel} sozlashda kutilmagan xato: {e}")
        return False

def _bitrate_looks_valid(channel: str, timeout: float, min_valid_frames: int) -> bool:
    test_bus = None
    try:
        test_bus = can.interface.Bus(channel=channel, interface=INTERFACE, receive_own_messages=False)
        valid_count = 0
        error_count = 0
        deadline = time.time() + timeout

        while time.time() < deadline:
            msg = test_bus.recv(timeout=0.3)
            if msg is None:
                continue
            if getattr(msg, "is_error_frame", False):
                error_count += 1
                continue
            valid_count += 1
            if valid_count >= min_valid_frames:
                return True

        return valid_count > 0 and error_count == 0

    except Exception as e:
        print(f"[DETECT] Tinglashda xato: {e}")
        return False
    finally:
        if test_bus is not None:
            try:
                test_bus.shutdown()
            except Exception:
                pass

def detect_bitrate(channel: str, candidates=None) -> int:
    candidates = candidates or CANDIDATE_BITRATES
    print(f"[DETECT] {channel} uchun bitrate aniqlanmoqda: {candidates}")

    for bitrate in candidates:
        print(f"[DETECT] {bitrate} sinovdan o'tkazilmoqda...")
        if not _configure_can_bitrate(channel, bitrate):
            continue
        if _bitrate_looks_valid(channel, BITRATE_DETECT_TIMEOUT, BITRATE_MIN_VALID_FRAMES):
            print(f"[DETECT] Bitrate aniqlandi: {bitrate}")
            return bitrate

    fallback = candidates[0]
    print(f"[DETECT] Hech qanday bitrate tasdiqlanmadi, {fallback} bitrate bilan davom etiladi")
    _configure_can_bitrate(channel, fallback)
    return fallback

def ensure_can_interface(bitrate: int = None):
    bitrate = bitrate or BITRATE
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

        print(f"[INFO] {CHANNEL} down. Bringing it up with bitrate={bitrate}...")

        if not _configure_can_bitrate(CHANNEL, bitrate, DBITRATE, USE_FD):
            sys.exit(1)

        print(f"[SUCCESS] {CHANNEL} is now UP.")

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
    can_data["check_engine_status"] = "OFF"
    can_data["active_errors"] = []

    can_data["turn_signal_left"] = "OFF"
    can_data["turn_signal_right"] = "OFF"
    can_data["seat_belt"] = "N/A"
    can_data["steering_angle_rad"] = None

    fuel_tank_1 = None
    fuel_tank_2 = None
    trip_start_total_distance_miles = None

    vin_buffer = bytearray()
    vin_expected_size = 0
    vin_done = False
    vin_tp_active = False

def decode_dm1(d: bytes):
    if len(d) < 2:
        return "OFF", []

    lamp_status_byte = d[0]
    mil = (lamp_status_byte >> 6) & 0x03  # Malfunction Indicator Lamp
    rsl = (lamp_status_byte >> 4) & 0x03  # Red Stop Lamp
    awl = (lamp_status_byte >> 2) & 0x03  # Amber Warning Lamp (Check Engine)
    pl  = lamp_status_byte & 0x03         # Protect Lamp

    lamps_on = []
    if mil == 1: lamps_on.append("MIL (Emissions)")
    if rsl == 1: lamps_on.append("Red Stop")
    if awl == 1: lamps_on.append("Yellow Check")
    if pl == 1:  lamps_on.append("Protect Lamp")

    status_str = "ON: " + ", ".join(lamps_on) if lamps_on else "OFF"

    errors = []
    if len(d) >= 6:
        if d[2] != 0x00 or d[3] != 0x00 or (d[4] & 0xFC) != 0x00:
            spn = d[2] | (d[3] << 8) | ((d[4] & 0xE0) << 11)
            fmi = d[4] & 0x1F
            oc = d[5] & 0x7F
            
            if 0 < spn < 524287:
                errors.append(f"SPN:{spn} FMI:{fmi} OC:{oc}")
                
    return status_str, errors

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

def decode_turn_signal(d: bytes, bit_offset: int) -> str:
    """
    2-bit turn signal state.
    bit_offset — boshlang'ich bit pozitsiyasi (0-indexed, LSB first).

    Measured values (SPN 2368 / 2370):
      00 = De-activated (OFF)
      01 = Activated    (ON)
      10 = Fault Detected
      11 = Not Available
    """
    byte_idx = bit_offset // 8
    bit_pos  = bit_offset % 8
    if byte_idx >= len(d):
        return "N/A"
    val = (d[byte_idx] >> bit_pos) & 0x03
    return {0b00: "OFF", 0b01: "ON", 0b10: "FAULT", 0b11: "N/A"}.get(val, "N/A")

def decode_seat_belt(d: bytes) -> str:
    """
    SPN 1856 — PGN 57344
    Byte 0, bits 0-1:
      00 = NOT Buckled
      01 = Buckled
      10 = Error
      11 = Not Available
    """
    if len(d) < 1:
        return "N/A"
    val = d[0] & 0x03
    return {0b00: "NOT_BUCKLED", 0b01: "BUCKLED", 0b10: "ERROR", 0b11: "N/A"}.get(val, "N/A")

def decode_steering_angle(d: bytes) -> float | None:
    """
    SPN 1807 — PGN 61449
    Bytes 0-1 (little-endian), 2-byte unsigned int.
    Resolution : 1/1024 rad per bit
    Offset     : -31.374 rad
    0xFFFF      = Not Available
    """
    if len(d) < 2:
        return None
    raw = d[0] | (d[1] << 8)
    if raw == 0xFFFF:
        return None
    return round(raw / 1024.0 - 31.374, 4)

def request_pgn(bus, requested_pgn: int, label: str) -> None:
    pgn_bytes = [
        requested_pgn & 0xFF,
        (requested_pgn >> 8) & 0xFF,
        (requested_pgn >> 16) & 0xFF,
    ]

    msg = can.Message(
        arbitration_id=0x18EAFFF9,
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
    if msg_data[0] != 32:
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

_cached_bitrate = None

def create_bus():
    global _cached_bitrate

    if USE_FD:
        ensure_can_interface(BITRATE)
        return can.interface.Bus(
            channel=CHANNEL,
            interface=INTERFACE,
            bitrate=BITRATE,
            fd=True
        )

    if AUTO_DETECT_BITRATE:
        if _cached_bitrate is not None and _interface_is_up(CHANNEL):
            print(f"[DETECT] {CHANNEL} allaqachon UP, keshdagi {_cached_bitrate} bitrate ishlatiladi")
        else:
            _cached_bitrate = detect_bitrate(CHANNEL)
    else:
        ensure_can_interface(BITRATE)
        _cached_bitrate = BITRATE

    return can.interface.Bus(
        channel=CHANNEL,
        interface=INTERFACE
    )

def can_reader() -> None:
    global last_msg_time, was_off, trip_start_total_distance_miles
    global fuel_tank_1, fuel_tank_2
    global last_engine_hours_request_time, last_vin_request_time
    global _cached_bitrate

    while True:
        bus = None
        bus_had_traffic = False
        try:
            bus = create_bus()

            request_vin(bus)
            request_engine_hours(bus)
            now = time.time()
            last_engine_hours_request_time = now
            last_vin_request_time = now

            print("[INFO] CAN thread started")

            while True:
                msg = bus.recv(timeout=1)
                now = time.time()

                if msg:
                    bus_had_traffic = True

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
                        
                elif pgn == 65110:
                    d = decode_def(msg.data)
                    if d is not None:
                        can_data["def_level"] = d

                elif pgn == 65226:
                    lamp_status, active_dtcs = decode_dm1(msg.data)
                    can_data["check_engine_status"] = lamp_status
                    can_data["active_errors"] = active_dtcs

                elif pgn == 0xEC00:
                    start_vin_tp_if_matches(msg.data)

                elif pgn == 0xEB00:
                    consume_vin_tp_data(msg.data)

                elif pgn == 65248:
                    dist_km = decode_distance_km(msg.data)
                    if dist_km is not None:
                        dist_miles = km_to_miles(dist_km)
                        can_data["total_distance"] = round(dist_miles, 2)

                        if trip_start_total_distance_miles is None:
                            trip_start_total_distance_miles = dist_miles

                        if dist_miles >= trip_start_total_distance_miles:
                            can_data["trip_distance"] = round(
                                dist_miles - trip_start_total_distance_miles, 2
                            )

                elif pgn == 65253:
                    h = decode_engine_hours(msg.data)
                    if h is not None:
                        can_data["engine_hours"] = h

                elif pgn == 65088:
                    can_data["turn_signal_right"] = decode_turn_signal(msg.data, bit_offset=0)

                elif pgn == 65089:
                    can_data["turn_signal_left"] = decode_turn_signal(msg.data, bit_offset=0)

                elif pgn == 57344:
                    can_data["seat_belt"] = decode_seat_belt(msg.data)

                elif pgn == 61449:
                    angle = decode_steering_angle(msg.data)
                    if angle is not None:
                        can_data["steering_angle_rad"] = angle

                if fuel_tank_1 is not None and fuel_tank_2 is not None:
                    can_data["fuel_level"] = round((fuel_tank_1 + fuel_tank_2) / 2, 2)
                elif fuel_tank_1 is not None:
                    can_data["fuel_level"] = round(fuel_tank_1, 2)

        except Exception as e:
            print(f"[ERROR] CAN reader error: {e}")
            can_data["status"] = "OFF"
            if not bus_had_traffic:
                if _cached_bitrate is not None:
                    print("[DETECT] Traffik kelmadi, keshlangan bitrate bekor qilinmoqda")
                _cached_bitrate = None
            time.sleep(2)

        finally:
            try:
                if bus is not None:
                    bus.shutdown()
            except Exception:
                pass
