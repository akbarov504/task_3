import threading
from flask import Flask, jsonify
from can_decoder import can_reader, can_data
from gps_parse import gps_thread, gps_data

app = Flask(__name__)

@app.route("/api/telemetry", methods=["GET"])
def get_telemetry():
    print(can_data)
    print(gps_data)

    return jsonify({
        "can": can_data,
        "gps": gps_data
    })

if __name__ == "__main__":
    can_thread = threading.Thread(target=can_reader, daemon=True)
    gps_thread = threading.Thread(target=gps_thread, daemon=True)

    can_thread.start()
    gps_thread.start()

    app.run(port=8080, debug=True)
