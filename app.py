import can

def extract_pgn(arbitration_id):
    dp = (arbitration_id >> 24) & 0x01
    pf = (arbitration_id >> 16) & 0xFF
    ps = (arbitration_id >> 8) & 0xFF

    if pf < 240:
        return (dp << 16) | (pf << 8)
    return (dp << 16) | (pf << 8) | ps

def decode_rpm(data):
    raw = data[3] | (data[4] << 8)
    if raw >= 0xFFFA:
        return None
    return raw * 0.125

def decode_coolant_temp(data):
    raw = data[0]
    if raw == 0xFF:
        return None
    return raw - 40

def decode_oil_temp(data):
    raw = data[3]
    if raw == 0xFF:
        return None
    return raw - 40

def decode_vehicle_speed(data):
    raw = data[1] | (data[2] << 8)
    if raw == 0xFFFF:
        return None
    return raw * 0.00390625  # km/h

def decode_fuel_level(data):
    raw = data[1]
    if raw == 0xFF:
        return None
    return raw * 0.4  # %

def decode_battery_voltage(data):
    raw = data[0] | (data[1] << 8)
    if raw == 0xFFFF:
        return None
    return raw * 0.05  # Volt

bus = can.interface.Bus(channel="can0", interface="socketcan")

print("Listening J1939...")

for msg in bus:
    if not msg.is_extended_id:
        continue

    pgn = extract_pgn(msg.arbitration_id)

    # 🔧 RPM
    if pgn == 61444:
        rpm = decode_rpm(msg.data)
        if rpm:
            print(f"RPM: {rpm:.1f}")

    # 🌡 Coolant + Oil temp
    elif pgn == 65262:
        coolant = decode_coolant_temp(msg.data)
        oil = decode_oil_temp(msg.data)

        if coolant is not None:
            print(f"Coolant Temp: {coolant} °C")

        if oil is not None:
            print(f"Oil Temp: {oil} °C")

    # 🚗 Vehicle Speed
    elif pgn == 65265:
        speed = decode_vehicle_speed(msg.data)
        if speed is not None:
            print(f"Speed: {speed:.1f} km/h")

    # ⛽ Fuel Level
    elif pgn == 65276:
        fuel = decode_fuel_level(msg.data)
        if fuel is not None:
            print(f"Fuel Level: {fuel:.1f} %")

    # 🔋 Battery Voltage
    elif pgn == 65271:
        voltage = decode_battery_voltage(msg.data)
        if voltage is not None:
            print(f"Battery: {voltage:.1f} V")
