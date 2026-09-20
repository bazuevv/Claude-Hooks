"""Copy preview pixels or save original image bytes via Windows dialogs."""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

MAX_REQUEST_BYTES = 32 * 1024 * 1024
EXTENSIONS = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp", "bmp": "bmp", "avif": "avif"}
_name_lock = threading.Lock()
_last_name_second = 0
_name_sequence = 0


def image_filename(extension):
    """Local time, with distinct names even for simultaneous save requests."""
    global _last_name_second, _name_sequence
    with _name_lock:
        stamp = max(time.time_ns() // 1_000_000_000, _last_name_second)
        _name_sequence = _name_sequence + 1 if stamp == _last_name_second else 0
        _last_name_second = stamp
        suffix = f"-{_name_sequence + 1}" if _name_sequence else ""
    date = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d_%H-%M-%S")
    return f"ClaudeCode({date}){suffix}.{extension}"


def export_image(payload):
    if not isinstance(payload, dict) or payload.get("action") not in ("copy", "save"):
        raise ValueError("Unknown image action")
    source = payload.get("data_url", "")
    if not isinstance(source, str) or len(source) > MAX_REQUEST_BYTES:
        raise ValueError("Image is too large")
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp|bmp|avif);base64,([A-Za-z0-9+/=\r\n]+)", source)
    if not match:
        raise ValueError("Unsupported image data")
    encoded = match[2].replace("\r", "").replace("\n", "")
    data = base64.b64decode(encoded, validate=True)
    if not data:
        raise ValueError("Empty image")
    if payload["action"] == "copy" and match[1] != "png":
        raise ValueError("Clipboard requires PNG pixels")
    extension = EXTENSIONS[match[1]]
    name = image_filename(extension) if payload["action"] == "save" else "image.png"
    if os.name != "nt":
        raise RuntimeError("Native image export is available on Windows")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("image-export.ps1"))],
        input=json.dumps({"action": payload["action"], "base64": encoded, "name": name, "extension": extension}),
        capture_output=True, text=True, encoding="utf-8", timeout=300,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("Windows could not copy or save the image")
    reply = json.loads(result.stdout)
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise RuntimeError("Invalid image export response")
    return reply
