"""Apply the emoji patch and the Windows GPU rendering workaround, with backup."""
from pathlib import Path
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys

hooks = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("webview_patch", hooks / "patch-claude-webview.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
argv = Path.home() / ".vscode/argv.json"
original = argv.read_text(encoding="utf-8") if argv.exists() else "{}"
data = json.loads(patch._strip_jsonc(original))
if data.get("disable-hardware-acceleration") is not True:
    if argv.exists():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(argv, argv.with_name("argv.json.before-ui-fix-" + stamp))
    # Preserve comments and all existing runtime settings.
    commented = '// "disable-hardware-acceleration": true,'
    if commented in original and "disable-hardware-acceleration" not in data:
        updated = original.replace(commented, '"disable-hardware-acceleration": true,', 1)
    else:
        data["disable-hardware-acceleration"] = True
        updated = json.dumps(data, indent=2) + "\n"
    assert json.loads(patch._strip_jsonc(updated))["disable-hardware-acceleration"] is True
    argv.write_text(updated, encoding="utf-8")
env = dict(os.environ, CLAUDE_PROJECT_DIR=str(hooks.parent.parent), PYTHONUTF8="1")
subprocess.run([sys.executable, str(hooks / "patch-claude-webview.py")],
               input='{"hook_event_name":"SessionStart"}', text=True,
               encoding="utf-8", env=env, check=True,
               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
print("Emoji patch applied. Software rendering enabled for the next full VS Code start.")
