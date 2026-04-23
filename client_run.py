import socket
import json
import time

SOCKET_PATH = "/tmp/telemetry.sock"

while True:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(SOCKET_PATH)

        data = client.recv(65536)
        client.close()

        payload = json.loads(data.decode("utf-8"))

        print("CAN:", payload["can"])
        print("GPS:", payload["gps"])
        print("------")

    except Exception as e:
        print(f"[ERROR] {e}")

    time.sleep(1)  # har 1 sekundda yangilaydi
