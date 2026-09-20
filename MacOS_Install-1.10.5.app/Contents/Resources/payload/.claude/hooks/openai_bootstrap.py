"""Maintain the optional OpenAI profile and bridge while the hook server runs."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time

import account_switcher as accounts
import codex_bridge_manager as bridge
from codex_app_server import CodexAppServerClient, CodexAppServerError, _safe_probe
import hook_log

CHECK_INTERVAL = 30
_LOCK = threading.Lock()
_last_check = None
_last_result = (False, "OpenAI: проверка ещё не выполнена")


def create_profile(snapshot):
    """Publish a complete profile only if absent; never switch the active account."""
    profile = Path(accounts.CLAUDE_DIR) / "settings_openai.json"
    if profile.exists():
        return False
    if not snapshot.get("account"):
        raise ValueError("Войдите в Codex для автоматического добавления OpenAI")
    roles = accounts.openai_role_models(snapshot.get("models"))
    if not roles:
        raise ValueError("Codex не сообщил три поддерживаемые модели")
    url = f"http://{bridge.BRIDGE_HOST}:{bridge.BRIDGE_PORT}"
    try:
        active = accounts.read_settings(accounts.SETTINGS_FILE)
    except FileNotFoundError:
        active = {}
    env = active.get("env", {})
    if isinstance(env, dict) and str(env.get("ANTHROPIC_BASE_URL", "")).rstrip("/") == url:
        # Recover custom settings if the deleted profile is still active.
        data = active
    else:
        model = roles["ANTHROPIC_DEFAULT_OPUS_MODEL"]
        data = {"env": {
            "ANTHROPIC_BASE_URL": url,
            "ANTHROPIC_AUTH_TOKEN": "local-codex-bridge",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            **roles,
        }, "model": model}
    data = accounts.with_shared_settings(profile.parent, data)
    profile.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=profile.parent, prefix=".openai-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # Atomic create-if-absent on macOS, Linux and Windows/NTFS. Another
        # server or manual setup cannot overwrite an existing profile.
        try:
            os.link(temporary, profile)
        except FileExistsError:
            return False
    finally:
        os.unlink(temporary)
    return True


def ensure_ready(*, force=False):
    """Serialize checks and throttle retries, including when Codex is logged out."""
    global _last_check, _last_result
    if not _LOCK.acquire(blocking=False):
        return _last_result
    try:
        if not force and _last_check is not None and time.monotonic() - _last_check < CHECK_INTERVAL:
            return _last_result
        previous = _last_result
        try:
            snapshot = None
            if not (Path(accounts.CLAUDE_DIR) / "settings_openai.json").exists():
                with CodexAppServerClient() as client:
                    snapshot = _safe_probe(client.snapshot(include_rate_limits=False))
                if not snapshot.get("account"):
                    raise ValueError("Войдите в Codex для автоматического добавления OpenAI")
                if not accounts.openai_role_models(snapshot.get("models")):
                    raise ValueError("Codex не сообщил три поддерживаемые модели")
            ready, message = bridge.ensure()
            if ready and snapshot is not None:
                if create_profile(snapshot):
                    hook_log.log("openai", "создан settings_openai.json; активный аккаунт сохранён")
            _last_result = (ready, message)
        except (OSError, ValueError, CodexAppServerError) as exc:
            # RPC and filesystem errors may include private data: log only type.
            _last_result = (False, f"OpenAI: автонастройка недоступна ({type(exc).__name__}); повтор через {CHECK_INTERVAL} с")
        _last_check = time.monotonic()
        if _last_result != previous:
            hook_log.log("openai", _last_result[1])
        return _last_result
    finally:
        _LOCK.release()


def run_monitor(stop_event):
    while not stop_event.is_set():
        ensure_ready(force=True)
        if stop_event.wait(CHECK_INTERVAL):
            return
