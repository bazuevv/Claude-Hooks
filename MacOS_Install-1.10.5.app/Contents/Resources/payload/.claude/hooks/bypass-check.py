"""Honor the session's explicit ByPass marker without requiring jq on Windows."""
import json
import os
from pathlib import Path
import re
import sys
from hook_paths import bypass_dir


def decision(data, project):
    sid = data.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        return None
    if not project or not (bypass_dir(project) / sid).is_file():
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "ByPass explicitly enabled for this session",
    }}


def main():
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            return
        result = decision(data, os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd"))
        if result:
            print(json.dumps(result))
    except (OSError, ValueError, TypeError):
        return


if __name__ == "__main__":
    main()
