import socket
import json

SOCKET_PATH = "/tmp/telemetry.sock"

client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
client.connect(SOCKET_PATH)

data = client.recv(65536)
client.close()

payload = json.loads(data.decode("utf-8"))
print(payload)
