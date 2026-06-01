import requests

def get_shared_token():
    response = requests.get("http://127.0.0.1:8787/token", timeout=5)
    response.raise_for_status()
    return response.json()["token"]
