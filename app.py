import threading
import time
import gps_parse
import socket
import os
import json
from can_decoder import can_reader, can_data
from moving import poll_forever

UNIX_SOCKET_PATH = "/tmp/telemetry.sock"

def build_telemetry_payload():
    return {
        "can": can_data,
        "gps": {
            "latitude": gps_parse.get_latitude(),
            "longitude": gps_parse.get_longitude(),
            "speed_mph": gps_parse.get_speed_mph(),
            "direction": gps_parse.get_direction(),
            "degree": gps_parse.get_degree(),
            "state": gps_parse.get_state(),
            "state_code": gps_parse.get_state_code()
        }
    }

def unix_socket_server():
    if os.path.exists(UNIX_SOCKET_PATH):
        os.remove(UNIX_SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(UNIX_SOCKET_PATH)
    server.listen(5)

    print(f"[INFO] UNIX socket listening: {UNIX_SOCKET_PATH}")

    while True:
        
        conn = None
        try:
            conn, _ = server.accept()
            payload = build_telemetry_payload()
            response = json.dumps(payload).encode("utf-8")
            conn.sendall(response)

        except Exception as e:
            print(f"[ERROR] UNIX socket server error: {e}")
        finally:
            try:
                if conn:
                    conn.close()
            except Exception:
                pass

can_thread = threading.Thread(target=can_reader, daemon=True)
can_thread.start()

unix_socket_thread = threading.Thread(target=unix_socket_server, daemon=True)
unix_socket_thread.start()

while True:
    poll_forever(build_telemetry_payload())
    print("CAN Data:", can_data)
    print(f"Lat: {gps_parse.get_latitude():.6f}, Lon: {gps_parse.get_longitude():.6f}, Speed: {gps_parse.get_speed_mph():.1f} mph, Dir: {gps_parse.get_direction()} ({gps_parse.get_degree()}°), State: {gps_parse.get_state()} ({gps_parse.get_state_code()})")
    time.sleep(1)
