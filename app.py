import threading
import gps_parse
from flask import Flask, jsonify
from can_decoder import can_reader, can_data

app = Flask(__name__)

@app.route("/api/telemetry", methods=["GET"])
def get_telemetry():
    gps_info = {
        "latitude": gps_parse.get_latitude(),
        "longitude": gps_parse.get_longitude(),
        "speed": gps_parse.get_speed_mph(),
        "direction": gps_parse.get_direction(),
        "degree": gps_parse.get_degree(),
        "state": gps_parse.get_state(),
        "state_code": gps_parse.get_state_code()
    }

    return jsonify({
        "can": can_data,
        "gps": gps_info
    })

if __name__ == "__main__":
    can_thread = threading.Thread(target=can_reader, daemon=True)
    can_thread.start()

    app.run(port=8080, debug=True)
