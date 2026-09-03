from __future__ import annotations

import os
import socket
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).parent / ".env"

load_dotenv(ENV_FILE)

def _get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

DAQ_HOST = _get("DAQ_HOST", "192.168.1.129")
GRPC_PORT = int(_get("GRPC_PORT", "31763"))

def _own_ip(toward: str, fallback: str = "127.0.0.1") -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((toward, 1))
        return s.getsockname()[0]
    except OSError:
        return fallback
    finally:
        s.close()

HOST = _own_ip(DAQ_HOST)

GRPC_TARGET = f"{DAQ_HOST}:{GRPC_PORT}"

DASHBOARD_URL = f"http://{HOST}"

SYSTEMLINK_URI = _get("SYSTEMLINK_URI", f"https://{DAQ_HOST}")
SYSTEMLINK_USER = _get("SYSTEMLINK_USER")
SYSTEMLINK_PASSWORD = _get("SYSTEMLINK_PASSWORD")
SYSTEMLINK_WORKSPACE = _get("SYSTEMLINK_WORKSPACE") or None
SYSTEMLINK_VERIFY_TLS = _get("SYSTEMLINK_VERIFY_TLS", "false").lower() == "true"
SYSTEMLINK_TIMEOUT_S = float(_get("SYSTEMLINK_TIMEOUT_S", "60"))
