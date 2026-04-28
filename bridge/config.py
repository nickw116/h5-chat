import os

GW_URL = os.getenv("GW_URL", "ws://127.0.0.1:22533")
GW_TOKEN = os.getenv("GW_TOKEN", "")
DEVICE_KEY_PATH = os.getenv("DEVICE_KEY_PATH", "/root/.openclaw/h5-bridge-device.json")
H5_USER_TOKEN = os.getenv("H5_USER_TOKEN", "")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8080"))

if not GW_TOKEN:
    raise RuntimeError("环境变量 GW_TOKEN 未设置，请在 .env 文件中配置或导出该变量")
if not H5_USER_TOKEN:
    raise RuntimeError("环境变量 H5_USER_TOKEN 未设置，请在 .env 文件中配置或导出该变量")
