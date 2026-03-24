import can
import time
import json


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

# status / timing
last_msg_time = 0
was_off = True

# trip logic
trip_start_total_distance = None

# fuel tanks
fuel_tank_1 = None
fuel_tank_2 = None

# VIN multi-frame
vin_buffer = bytearray()
vin_expected_size = 0
vin_done = False


def extract_pgn(arbitration_id: int) -> int:
    pf = (arbitration_id >> 16) & 0xFF
    ps = (arbitration_id >> 8) & 0xFF

    if pf < 240:
        return pf << 8
    return (pf << 8) | ps


def kmh_to_mph(kmh: float) -> float:
    return kmh * 0.621371


def reset_runtime_fields():
    global fuel_tank_1, fuel_tank_2, trip_start_total_distance

    data["vehicle_speed"] = 0.0
    data["engine_speed"] = None
    data["wheel_based_speed"] = 0.0
    data["fuel_level"] = 0.0
    data["trip_distance"] = 0.0
    data["engine_load"] = None
    data["engine_temp"] = None
    data["status"] = "OFF"

    fuel_tank_1 = None
    fuel_tank_2 = None
    trip_start_total_distance = None


def decode_rpm(d: bytes):
    if len(d) < 5:
        return None
    raw = d[3] | (d[4] << 8)
    return None if raw >= 0xFFFA else raw * 0.125


def decode_engine_load(d: bytes):
    if len(d) < 3:
        return None
    raw = d[2]
    return None if raw == 0xFF else float(raw)


def decode_speed_kmh(d: bytes):
    if len(d) < 3:
        return None
    raw = d[1] | (d[2] << 8)
    return None if raw == 0xFFFF else raw * 0.00390625


def decode_temp(d: bytes):
    if len(d) < 1:
        return None
    raw = d[0]
    return None if raw == 0xFF else raw - 40


def decode_fuel(d: bytes):
    if len(d) < 2:
        return None
    raw = d[1]
    return None if raw == 0xFF else raw * 0.4


def decode_distance(d: bytes):
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
    raw = d[0]
    return None if raw == 0xFF else raw * 0.4


def request_vin(bus):
    msg = can.Message(
        arbitration_id=0x18EAFF00,   # Request PGN to global
        data=[0xEC, 0xFE, 0x00] + [0xFF] * 5,   # Request VIN PGN 0xFEEC
        is_extended_id=True
    )
    bus.send(msg)
    print("VIN requested")


bus = can.interface.Bus(channel="can0", interface="socketcan")

request_vin(bus)
print("FINAL TELEMETRY SYSTEM STARTED\n")

while True:
    msg = bus.recv(timeout=1)
    now = time.time()

    # OFF detection
    if msg is None:
        if now - last_msg_time > 3 and data["status"] != "OFF":
            reset_runtime_fields()
            was_off = True
            print(json.dumps(data, indent=4))
        continue

    # message keldi
    last_msg_time = now

    if not msg.is_extended_id:
        continue

    # OFF -> ON transition
    if was_off:
        data["status"] = "ON"
        was_off = False

        # trip yangi boshlanadi:
        # total_distance birinchi kelganda start nuqta bo'ladi
        trip_start_total_distance = None
    else:
        data["status"] = "ON"

    pgn = extract_pgn(msg.arbitration_id)

    # 61444 = EEC1
    if pgn == 61444:
        data["engine_speed"] = decode_rpm(msg.data)
        data["engine_load"] = decode_engine_load(msg.data)

    # 65265 = CCVS (wheel-based vehicle speed)
    elif pgn == 65265:
        speed_kmh = decode_speed_kmh(msg.data)
        if speed_kmh is not None:
            speed_mph = kmh_to_mph(speed_kmh)
            data["vehicle_speed"] = speed_mph
            data["wheel_based_speed"] = speed_mph

    # 65262 = ET1
    elif pgn == 65262:
        temp = decode_temp(msg.data)
        if temp is not None:
            data["engine_temp"] = temp

    # 65276 = fuel tank 1 deb olayapmiz
    elif pgn == 65276:
        fuel = decode_fuel(msg.data)
        if fuel is not None:
            fuel_tank_1 = fuel

    # 65277 = fuel tank 2 deb olayapmiz
    elif pgn == 65277:
        fuel = decode_fuel(msg.data)
        if fuel is not None:
            fuel_tank_2 = fuel

    # fuel average logic
    if fuel_tank_1 is not None and fuel_tank_2 is not None:
        data["fuel_level"] = (fuel_tank_1 + fuel_tank_2) / 2
    elif fuel_tank_1 is not None:
        data["fuel_level"] = fuel_tank_1

    # 65248 = distance
    elif pgn == 65248:
        dist = decode_distance(msg.data)
        if dist is not None:
            data["total_distance"] = dist

            # yangi ON bo'lganda birinchi total_distance trip start bo'ladi
            if trip_start_total_distance is None:
                trip_start_total_distance = dist

            if dist >= trip_start_total_distance:
                data["trip_distance"] = dist - trip_start_total_distance
            else:
                # agar ECU distance kamayib qolsa, tripni buzmaymiz
                data["trip_distance"] = 0.0

    # 65253 = engine hours
    elif pgn == 65253:
        hours = decode_engine_hours(msg.data)
        if hours is not None:
            data["engine_hours"] = hours

    # 65110 = DEF level (simulator/proprietary bo'lishi mumkin)
    elif pgn == 65110:
        d = decode_def(msg.data)
        if d is not None:
            data["def_level"] = d

    # TP.CM
    elif pgn == 0xEC00:
        control_byte = msg.data[0]
        if control_byte == 32:  # BAM
            vin_expected_size = msg.data[1] | (msg.data[2] << 8)
            vin_buffer = bytearray()
            vin_done = False

    # TP.DT
    elif pgn == 0xEB00:
        if not vin_done:
            vin_buffer.extend(msg.data[1:])
            if len(vin_buffer) >= vin_expected_size:
                try:
                    vin_text = vin_buffer[:vin_expected_size].decode(errors="ignore").strip()
                    data["vin"] = vin_text
                except Exception:
                    pass
                vin_done = True

    print(json.dumps(data, indent=4))