import os

GW_URL = os.getenv("GW_URL", "ws://127.0.0.1:22533")
GW_TOKEN = os.getenv("GW_TOKEN", "REDACTED_GW_TOKEN")
DEVICE_KEY_PATH = os.getenv("DEVICE_KEY_PATH", "/root/.openclaw/h5-bridge-device.json")
H5_USER_TOKEN = os.getenv("H5_USER_TOKEN", "REDACTED_H5_USER_TOKEN")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8080"))
