import serial
import pynmea2
import time
import threading
import os
import json

PORT = "/dev/ttyUSB3"
BAUD = 115200

# Shared data (will be updated by background thread)
_gps_data = {
    "lat": 0.0,
    "lon": 0.0,
    "speed_mph": 0.0,
    "direction": "NW",
    "degree": 0.0,
    "state": "N/A",
    "state_code": "N/A"
}

# --- CONFIGURATION (Matching gps_parse.py) ---
# Primary absolute path
GEOJSON_DIR_ABS = "/usr/local/share/geoJSON"
# Fallback relative path
GEOJSON_DIR_REL = "./geoJSON"

_state_boundaries = []

def _is_point_in_poly(x, y, poly):
    """
    Ray-casting algorithm to check if point (x,y) is inside polygon.
    x: longitude, y: latitude
    poly: list of [lon, lat] points
    """
    n = len(poly)
    inside = False
    p1x, p1y = poly[0]
    for i in range(n + 1):
        p2x, p2y = poly[i % n]
        if y > min(p1y, p2y):
            if y <= max(p1y, p2y):
                if x <= max(p1x, p2x):
                    if p1y != p2y:
                        xinters = (y - p1y) * (p2x - p1x) / (p2y - p1y) + p1x
                    if p1x == p2x or x <= xinters:
                        inside = not inside
        p1x, p1y = p2x, p2y
    return inside

def _load_boundaries():
    """Loads GeoJSON files from directory and calculates bounding boxes for speed optimization."""
    global _state_boundaries
    
    # Determine which directory to use
    target_dir = GEOJSON_DIR_ABS
    if not os.path.exists(target_dir):
        if os.path.exists(GEOJSON_DIR_REL):
            target_dir = GEOJSON_DIR_REL
        else:
            print(f"[Error] GeoJSON directory not found at {GEOJSON_DIR_ABS} or {GEOJSON_DIR_REL}")
            return

    print(f"Loading state boundaries from {target_dir}...")
    count = 0
    
    try:
        files = sorted(os.listdir(target_dir))
    except OSError:
        print(f"[Error] Accessing directory {target_dir}")
        return

    for fname in files:
        if fname.endswith(".geojson"):
            try:
                with open(os.path.join(target_dir, fname), "r") as f:
                    data = json.load(f)
                    
                    if data.get("type") == "FeatureCollection":
                         features = data.get("features", [])
                         if features: data = features[0]

                    props = data.get("properties", {})
                    name = props.get("name", "Unknown")
                    abbr = props.get("abbreviation", "UNK")
                    geometry = data.get("geometry", {})
                    
                    min_x, min_y, max_x, max_y = 180.0, 90.0, -180.0, -90.0
                    
                    def update_bbox(ring):
                        nonlocal min_x, min_y, max_x, max_y
                        for p in ring:
                            px, py = p
                            if px < min_x: min_x = px
                            if px > max_x: max_x = px
                            if py < min_y: min_y = py
                            if py > max_y: max_y = py
                            
                    gtype = geometry.get("type")
                    coords = geometry.get("coordinates")
                    
                    valid = False
                    if gtype == "Polygon":
                        for ring in coords: update_bbox(ring)
                        valid = True
                    elif gtype == "MultiPolygon":
                        for poly in coords:
                            for ring in poly: update_bbox(ring)
                        valid = True
                        
                    if valid:
                        _state_boundaries.append({
                            "name": name,
                            "abbr": abbr,
                            "geometry": geometry,
                            "bbox": (min_x, min_y, max_x, max_y)
                        })
                        count += 1
            except Exception as e:
                print(f"Error loading {fname}: {e}")
    print(f"Loaded {count} state boundaries.")

def get_state_and_code(lat, lon):
    """Finds state by checking if point is inside loaded polygons."""
    if not _state_boundaries:
        return "N/A", "N/A"
        
    for state in _state_boundaries:
        # 1. Fast Bounding Box Check
        min_x, min_y, max_x, max_y = state["bbox"]
        if not (min_x <= lon <= max_x and min_y <= lat <= max_y):
            continue
            
        # 2. Precise Polygon Check
        geom = state["geometry"]
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        
        found = False
        if gtype == "Polygon":
            if _is_point_in_poly(lon, lat, coords[0]):
                found = True
        elif gtype == "MultiPolygon":
            for poly in coords:
                if _is_point_in_poly(lon, lat, poly[0]):
                    found = True
                    break
        
        if found:
            return state["name"], state["abbr"]

    return "N/A", "N/A"

def _degrees_to_direction(deg):
    if deg is None:
        return "NW"
    deg = float(deg)
    if (deg >= 337.5) or (deg < 22.5):
        return "N"
    elif deg < 67.5:
        return "NE"
    elif deg < 112.5:
        return "E"
    elif deg < 157.5:
        return "SE"
    elif deg < 202.5:
        return "S"
    elif deg < 247.5:
        return "SW"
    elif deg < 292.5:
        return "W"
    else:
        return "NW"

def _open_serial():
    while True:
        try:
            ser = serial.Serial(PORT, BAUD, timeout=1)
            return ser
        except Exception:
            time.sleep(2)

def gps_thread():
    ser = _open_serial()
    
    _load_boundaries()
    while True:
        try:
            line = ser.readline().decode('ascii', errors='replace').strip()
            if line.startswith('$GPRMC'):
                try:
                    msg = pynmea2.parse(line)

                    # Latitude / Longitude
                    _gps_data["lat"] = msg.latitude
                    _gps_data["lon"] = msg.longitude

                    # Speed
                    speed_knots = msg.spd_over_grnd
                    if speed_knots is None or speed_knots == "":
                        speed_knots = 0.0
                    else:
                        speed_knots = float(speed_knots)
                    _gps_data["speed_mph"] = speed_knots * 1.15078

                    # Direction
                    _gps_data["direction"] = _degrees_to_direction(msg.true_course)
                    _gps_data["degree"] = msg.true_course
                    if not _state_boundaries:
                        print("No boundaries loaded. Exiting.")
                        _gps_data["state"], _gps_data["state_code"] = "N/A", "N/A"
                    else:
                        _gps_data["state"], _gps_data["state_code"] = get_state_and_code(msg.latitude, msg.longitude)
                except pynmea2.ParseError:
                    pass
        except Exception:
            ser.close()
            ser = _open_serial()

# 🧭 Public getter functions
def get_latitude():
    return _gps_data["lat"]

def get_longitude():
    return _gps_data["lon"]

def get_speed_mph():
    return _gps_data["speed_mph"]

def get_direction():
    return _gps_data["direction"]

def get_degree():
    return _gps_data["degree"]

def get_state():
    return _gps_data["state"]

def get_state_code():
    return _gps_data["state_code"]

if __name__ == "__main__":
    while True:
        print(f"Lat: {get_latitude():.6f}, Lon: {get_longitude():.6f}, Speed: {get_speed_mph():.1f} mph, Dir: {get_direction()} ({get_degree()}°), State: {get_state()} ({get_state_code()})")
        time.sleep(1)