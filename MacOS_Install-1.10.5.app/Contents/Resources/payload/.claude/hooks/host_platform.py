"""Host paths and process inspection for Windows, Linux and macOS."""

import json
import os
import subprocess
import sys


def vscode_data_dirs():
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return [os.path.join(base, product) for product in ("Code", "Code - Insiders")]


def process_command(pid):
    """Return a command line, or None when the process cannot be inspected.

    Callers must not terminate a process whose identity is unknown.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        if sys.platform == "win32":
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine | ConvertTo-Json -Compress",
            ], capture_output=True, text=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            command = json.loads(result.stdout) if result.returncode == 0 else None
            return command if isinstance(command, str) else None
        if sys.platform == "darwin":
            result = subprocess.run(
                ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            return (result.stdout.strip() or None) if result.returncode == 0 else None
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
