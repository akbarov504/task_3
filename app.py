import can
import time

# ---------------- DATA STORE ----------------

data = {
    "vehicle_speed": 0.0,
    "engine_speed": None,
    "wheel_based_speed": 0.0,
    "fuel_level": 0.0,
    "trip_distance": None,
    "total_distance": 0.0,
    "engine_load": None,
    "engine_temp": None,
    "vin": "",
    "def_level": 0.0,
    "engine_hours": 0.0,
    "status": "OFF"
}

last_msg_time = 0

# VIN buffer
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


# ---------------- DECODERS ----------------

def decode_rpm(d):
    raw = d[3] | (d[4] << 8)
    if raw >= 0xFFFA:
        return None
    return raw * 0.125


def decode_engine_load(d):
    raw = d[2]
    if raw == 0xFF:
        return None
    return raw


def decode_speed(d):
    raw = d[1] | (d[2] << 8)
    if raw == 0xFFFF:
        return None
    return raw * 0.00390625


def decode_temp(d):
    raw = d[0]
    if raw == 0xFF:
        return None
    return raw - 40


def decode_fuel(d):
    raw = d[1]
    if raw == 0xFF:
        return None
    return raw * 0.4


def decode_distance(d):
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    if raw == 0xFFFFFFFF:
        return None
    return raw * 0.125


def decode_engine_hours(d):
    raw = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
    if raw == 0xFFFFFFFF:
        return None
    return raw * 0.05


def decode_def(d):
    raw = d[0]
    if raw == 0xFF:
        return None
    return raw * 0.4


# ---------------- VIN REQUEST ----------------

def request_vin(bus):
    vin_pgn = [0xEC, 0xFE, 0x00]

    msg = can.Message(
        arbitration_id=0x18EAFF00,
        data=vin_pgn + [0xFF] * 5,
        is_extended_id=True
    )

    bus.send(msg)
    print("📤 VIN requested...")


# ---------------- MAIN ----------------

bus = can.interface.Bus(channel="can0", interface="socketcan")

request_vin(bus)

print("🚀 FULL TELEMETRY SYSTEM STARTED")

while True:
    msg = bus.recv(timeout=1)
    now = time.time()

    # STATUS LOGIC
    if msg:
        last_msg_time = now
        data["status"] = "ON"
    elif now - last_msg_time > 3:
        data["status"] = "OFF"

    if not msg:
        print(data)
        continue

    if not msg.is_extended_id:
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
            data["vehicle_speed"] = speed
            data["wheel_based_speed"] = speed

    # ---------------- TEMP ----------------
    elif pgn == 65262:
        temp = decode_temp(msg.data)
        if temp is not None:
            data["engine_temp"] = temp

    # ---------------- FUEL ----------------
    elif pgn == 65276:
        fuel = decode_fuel(msg.data)
        if fuel is not None:
            data["fuel_level"] = fuel

    # ---------------- DISTANCE ----------------
    elif pgn == 65248:
        dist = decode_distance(msg.data)
        if dist is not None:
            data["total_distance"] = dist
            data["trip_distance"] = dist

    # ---------------- ENGINE HOURS ----------------
    elif pgn == 65253:
        hours = decode_engine_hours(msg.data)
        if hours is not None:
            data["engine_hours"] = hours

    # ---------------- DEF ----------------
    elif pgn == 65110:
        d = decode_def(msg.data)
        if d is not None:
            data["def_level"] = d

    # ---------------- VIN MULTI-FRAME ----------------
    elif pgn == 0xEC00:  # TP.CM
        if msg.data[0] == 32:  # BAM
            vin_expected_size = msg.data[1] | (msg.data[2] << 8)
            vin_buffer = bytearray()
            vin_done = False

    elif pgn == 0xEB00:  # TP.DT
        if not vin_done:
            vin_buffer.extend(msg.data[1:])

            if len(vin_buffer) >= vin_expected_size:
                try:
                    vin = vin_buffer[:vin_expected_size].decode(errors="ignore")
                    data["vin"] = vin.strip()
                    print(f"🚗 VIN: {vin}")
                except:
                    pass

                vin_done = True

    print(data)