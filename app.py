import threading
import time
import gps_parse
from can_decoder import can_reader, can_data



can_thread = threading.Thread(target=can_reader, daemon=True)
can_thread.start()


while True:
    print(f"Lat: {gps_parse.get_latitude():.6f}, Lon: {gps_parse.get_longitude():.6f}, Speed: {gps_parse.get_speed_mph():.1f} mph, Dir: {gps_parse.get_direction()} ({gps_parse.get_degree()}°), State: {gps_parse.get_state()} ({gps_parse.get_state_code()})")
    time.sleep(1)
