import threading
from flask import Flask, jsonify
from can_decoder import can_reader, can_data
from gps_parse import start_gps_thread, _gps_data

app = Flask(__name__)

@app.route("/api/telemetry", methods=["GET"])
def get_telemetry():
    print(can_data)
    print(_gps_data)

    return jsonify({
        "can": can_data,
        "gps": _gps_data
    })

if __name__ == "__main__":
    start_gps_thread()
    can_thread = threading.Thread(target=can_reader, daemon=True)
    can_thread.start()

    app.run(port=8080, debug=True)
