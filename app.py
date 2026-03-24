import can
import time
import json

# ---------------- DATA ----------------

def init_data():
    return {
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
        "status": "OFF"
    }


data = init_data()

# runtime variables
last_msg_time = 0
last_total_distance = None
trip_distance = 0.0

fuel_tank_1 = None
fuel_tank_2 = None

# VIN
vin_buffer = bytearray()
vin_expected_size = 0
vin_done = False


# ---------------- HELPERS ----------------

def extract_pgn(arbitration_id):
    pf = (arbitration_id >> 16) & 0xFF
    ps = (arbitration_id >> 8) & 0xFF

    if pf < 240:
        return pf << 8
    return (pf << 8) | ps


def kmh_to_mph(kmh):
    return kmh * 0.621371


# ---------------- RESET (ONLY RUNTIME) ----------------

def reset_runtime():
    data["vehicle_speed"] = 0.0
    data["engine_speed"] = None
    data["wheel_based_speed"] = 0.0
    data["engine_load"] = None
    data["engine_temp"] = None
    data["fuel_level"] = 0.0
    data["status"] = "OFF"


# ---------------- DECODERS ----------------

def decode_rpm(d):
    raw = d[3] | (d[4] << 8)
    return None if raw >= 0xFFFA else raw * 0.125


def decode_engine_load(d):
    return None if d[2] == 0xFF else d[2]


def decode_speed(d):
    raw = d[1] | (d[2] << 8)
    return None if raw == 0xFFFF else raw * 0.00390625


def decode_temp(d):
    return None if d[0] == 0xFF else d[0] - 40


def decode_fuel(d):
    return None if d[1] == 0xFF else d[1] * 0.4


def decode_distance(d):
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    return None if raw == 0xFFFFFFFF else raw * 0.125


def decode_engine_hours(d):
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    return None if raw == 0xFFFFFFFF else raw * 0.05


def decode_def(d):
    return None if d[0] == 0xFF else d[0] * 0.4


# ---------------- VIN REQUEST ----------------

def request_vin(bus):
    msg = can.Message(
        arbitration_id=0x18EAFF00,
        data=[0xEC, 0xFE, 0x00] + [0xFF] * 5,
        is_extended_id=True
    )
    bus.send(msg)
    print("📤 VIN requested")


# ---------------- MAIN ----------------

bus = can.interface.Bus(channel="can0", interface="socketcan")

request_vin(bus)

print("🚀 FINAL TELEMETRY SYSTEM RUNNING\n")

while True:
    msg = bus.recv(timeout=1)
    now = time.time()

    # ---------------- STATUS ----------------
    if msg:
        last_msg_time = now
        data["status"] = "ON"
    elif now - last_msg_time > 3:
        reset_runtime()
        print(json.dumps(data, indent=4))
        continue

    if not msg or not msg.is_extended_id:
        continue

    pgn = extract_pgn(msg.arbitration_id)

    # ---------------- ENGINE ----------------
    if pgn == 61444:
        data["engine_speed"] = decode_rpm(msg.data)
        data["engine_load"] = decode_engine_load(msg.data)

    # ---------------- SPEED ----------------
    elif pgn == 65265:
        speed = decode_speed(msg.data)
        if speed is not None:
            mph = kmh_to_mph(speed)
            data["vehicle_speed"] = mph
            data["wheel_based_speed"] = mph

    # ---------------- TEMP ----------------
    elif pgn == 65262:
        temp = decode_temp(msg.data)
        if temp is not None:
            data["engine_temp"] = temp

    # ---------------- FUEL ----------------
    elif pgn == 65276:
        f = decode_fuel(msg.data)
        if f is not None:
            fuel_tank_1 = f

    elif pgn == 65277:
        f = decode_fuel(msg.data)
        if f is not None:
            fuel_tank_2 = f

    # fuel avg logic
    if fuel_tank_1 is not None and fuel_tank_2 is not None:
        data["fuel_level"] = (fuel_tank_1 + fuel_tank_2) / 2
    elif fuel_tank_1 is not None:
        data["fuel_level"] = fuel_tank_1

    # ---------------- DISTANCE ----------------
    elif pgn == 65248:
        dist = decode_distance(msg.data)

        if dist is not None:
            data["total_distance"] = dist

            if last_total_distance is not None:
                delta = dist - last_total_distance
                if delta > 0:
                    trip_distance += delta

            last_total_distance = dist
            data["trip_distance"] = trip_distance

    # ---------------- ENGINE HOURS ----------------
    elif pgn == 65253:
        h = decode_engine_hours(msg.data)
        if h is not None:
            data["engine_hours"] = h

    # ---------------- DEF ----------------
    elif pgn == 65110:
        d = decode_def(msg.data)
        if d is not None:
            data["def_level"] = d

    # ---------------- VIN ----------------
    elif pgn == 0xEC00:
        if msg.data[0] == 32:
            vin_expected_size = msg.data[1] | (msg.data[2] << 8)
            vin_buffer = bytearray()
            vin_done = False

    elif pgn == 0xEB00:
        if not vin_done:
            vin_buffer.extend(msg.data[1:])
            if len(vin_buffer) >= vin_expected_size:
                try:
                    data["vin"] = vin_buffer[:vin_expected_size].decode(errors="ignore").strip()
                except:
                    pass
                vin_done = True

    # ---------------- OUTPUT ----------------
    print(json.dumps(data, indent=4))