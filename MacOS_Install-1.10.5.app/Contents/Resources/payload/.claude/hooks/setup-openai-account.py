"""Add the current Codex login to Accs without copying OAuth credentials."""
import json
from pathlib import Path

import codex_bridge_manager
import account_switcher
from codex_app_server import CodexAppServerClient, _safe_probe
from openai_bootstrap import create_profile


def main():
    with CodexAppServerClient() as client:
        snapshot = _safe_probe(client.snapshot(include_rate_limits=False))
    if not snapshot.get("account"):
        raise SystemExit("Sign in to Codex first")
    roles = account_switcher.openai_role_models(snapshot.get("models"))
    if not roles:
        raise SystemExit("Codex did not report three supported models; settings left unchanged")
    ready, message = codex_bridge_manager.ensure()
    if not ready:
        raise SystemExit(message)
    profile = Path(account_switcher.CLAUDE_DIR) / "settings_openai.json"
    create_profile(snapshot)
    account_switcher.sync_openai_models(snapshot["models"])
    print("OpenAI profile:", profile)
    print("Codex account:", snapshot["account"].get("email"))
    print("Role models:", json.dumps(roles))


if __name__ == "__main__":
    main()
