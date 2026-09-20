"""Install the project's UI hooks and activate an existing API profile on Windows.

Run manually once before the first authenticated session. SessionStart cannot
bootstrap the account selector while the extension is still at its login page.
Existing settings and extension bundles are backed up before installation.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    if os.name != "nt":
        raise SystemExit("This bootstrap is for Windows only")
    hooks = Path(__file__).resolve().parent
    project = hooks.parent.parent
    claude = Path.home() / ".claude"
    profile = claude / "settings_api.json"
    data = json.loads(profile.read_text(encoding="utf-8-sig"))
    if not any(data.get("env", {}).get(k) for k in
               ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")):
        raise SystemExit("settings_api.json has no API credentials")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = claude / "backups" / ("windows-hooks-" + stamp)
    backup.mkdir(parents=True)
    for name in ("settings.json", "settings.json.bak", ".active-account"):
        source = claude / name
        if source.exists():
            shutil.copy2(source, backup / name)
    for extension in (Path.home() / ".vscode/extensions").glob("anthropic.claude-code-*"):
        for name in ("package.json", "extension.js", "webview/index.js"):
            source = extension / name
            if source.is_file():
                target = backup / extension.name / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    print("Backup:", backup, flush=True)
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")
    for name in ("patch-extension-csp.py", "localize.py",
                 "patch-extension-settings.py", "patch-claude-webview.py"):
        result = subprocess.run([sys.executable, str(hooks / name)],
                                input=json.dumps({"hook_event_name": "SessionStart"}),
                                text=True, encoding="utf-8", env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise SystemExit(f"{name} failed: {result.returncode}")
    import account_switcher
    ok, message = account_switcher.switch_account("settings_api.json")
    if not ok:
        raise SystemExit(message)
    print("API profile activated; reload the VS Code window.")


if __name__ == "__main__":
    main()
