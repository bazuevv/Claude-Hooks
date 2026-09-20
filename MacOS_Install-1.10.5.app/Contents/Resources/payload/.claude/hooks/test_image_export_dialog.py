"""Explicit Windows UI smoke test: open Save image and cancel, without writing."""
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time


def main():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    process = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("image-export.ps1"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
    )
    payload = json.dumps({"action": "save", "base64": "dGVzdA==", "name": "dialog-test.png", "extension": "png"})
    found = []

    @callback_type
    def inspect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == process.pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if title.value == "Save image":
                found.append(hwnd)
        return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        reply = executor.submit(process.communicate, payload, timeout=20)
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and not found and process.poll() is None:
                user32.EnumWindows(inspect, 0)
                time.sleep(0.1)
            assert found, "Save dialog did not become visible"
            assert user32.GetWindow(found[0], 4), "Save dialog has no owner"
            assert user32.PostMessageW(found[0], 0x111, 2, 0), "Could not cancel test dialog"
            stdout, stderr = reply.result(timeout=5)
            assert process.returncode == 0, stderr
            assert json.loads(stdout) == {"ok": True, "cancelled": True}, stdout
            print("Save dialog visible with owner; Cancel completes request; no file written: OK")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
