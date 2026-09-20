"""Probe Write routing through the bridge without executing a filesystem write."""
import json
import urllib.request
import uuid
from pathlib import Path
import codex_bridge_manager

ok, message = codex_bridge_manager.ensure()
if not ok:
    raise SystemExit(message)
profile = json.loads((Path.home() / ".claude/settings_openai.json").read_text(encoding="utf-8"))
payload = {
    "model": profile["env"]["ANTHROPIC_MODEL"],
    "max_tokens": 500,
    "stream": False,
    "metadata": {"user_id": "bridge-write-probe-" + uuid.uuid4().hex},
    "messages": [{"role": "user", "content":
        "Create D:/Project/KAYF-LIFE/.claude/hooks-runtime/bridge-check.txt with the text OK."}],
    "tools": [{"name": "Write", "description":
        "Write a file using the Claude Code host. The host enforces file permissions.",
        "input_schema": {"type": "object", "properties": {
            "file_path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["file_path", "content"]}}],
}
request = urllib.request.Request(
    f"http://127.0.0.1:{codex_bridge_manager.BRIDGE_PORT}/v1/messages",
    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=90) as response:
    result = json.load(response)
calls = [b for b in result.get("content", []) if b.get("type") == "tool_use"]
print("Requested host tools:", [b.get("name") for b in calls])
if not any(b.get("name") == "Write" for b in calls):
    print("Model response:", json.dumps(result.get("content"), ensure_ascii=True))
    raise SystemExit(1)
print("Write routed to Claude Code successfully. The probe did not execute the write.")
