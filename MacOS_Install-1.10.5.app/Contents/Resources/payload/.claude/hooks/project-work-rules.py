"""Inject the project workflow rules before each user task."""
import json
import os
import sys
from pathlib import Path

try:
    data = json.load(sys.stdin)
except (ValueError, OSError):
    data = {}
project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd")
               or Path(__file__).resolve().parents[2])
rules = [project / "CLAUDE.md", project / ".claude" / "Readme.md"]
context = "\n\n".join(path.read_text(encoding="utf-8") for path in rules if path.is_file())
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }
}, ensure_ascii=True))
