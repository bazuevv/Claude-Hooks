"""Collect idle account quotas in the existing local server, independently of tabs."""
import glob
import os
import sqlite3
import threading
import time
import tomllib
from contextlib import closing
from datetime import datetime
from pathlib import Path

import account_switcher
import account_usage_history
import hook_log
import message_costs
import usage_history_store

CONFIG = Path(__file__).resolve().parent.parent / "patches" / "claude-custom-config.toml"
_lock = threading.Lock()
_seen = {}
_attempts = {}


def settings():
    try:
        with CONFIG.open("rb") as source:
            cfg = tomllib.load(source)
        interval = float(cfg.get("accountUsageBackgroundIntervalSec", 3600))
        if not 60 <= interval <= 86400:
            interval = 3600
        return cfg.get("accountUsageBackgroundEnabled", True) is True, interval
    except (OSError, ValueError, TypeError):
        return False, 3600


def _remember(sample):
    stamps = [w["observed_at"] for w in sample.get("windows", []) if not w.get("expired")]
    if sample.get("account") and stamps:
        with _lock:
            _seen[sample["account_file"]] = (sample["account"], max(stamps))


def record_entry(entry, sources, state_dir):
    """Reuse the exact Accs response; never issue a second quota request."""
    try:
        sample = message_costs.capture(entry["file"], **sources)
        if sample["billing"] != "subscription" or not sample.get("account") or not sample["windows"]:
            return
        subscription = entry.get("subscription") or {}
        if subscription.get("paidAt") and subscription.get("until"):
            sample["subscriptionPeriod"] = {
                "start": datetime.fromisoformat(subscription["paidAt"]).astimezone().isoformat(),
                "end": datetime.fromisoformat(subscription["until"]).astimezone().isoformat()}
        usage_history_store.record_account(state_dir, {**sample, "source": "accs"})
        _remember(sample)
    except Exception as exc:
        hook_log.log("server", f"quota history: collection failed ({type(exc).__name__})")


def last_observation(filename):
    with _lock:
        account, stamp = _seen.get(filename, (None, 0))
    if not account:
        return stamp
    database = usage_history_store.ROOT / "quota-periods.sqlite3"
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            value = db.execute("SELECT MAX(last_seen) FROM quota_cycles WHERE account=?", (account,)).fetchone()[0]
        return max(stamp, value or 0)
    except (OSError, sqlite3.Error):
        return stamp


def poll(state_dir, *, now=None):
    enabled, interval = settings()
    if not enabled:
        return
    now = time.time() if now is None else now
    for path in sorted(glob.glob(os.path.join(account_switcher.CLAUDE_DIR, "settings*.json"))):
        filename = os.path.basename(path)
        if filename in account_switcher.EXCLUDED or not account_switcher.ACCOUNT_NAME_RE.match(filename):
            continue
        # Failed requests also wait one interval; no retries every UI tick.
        if now - max(last_observation(filename), _attempts.get(filename, 0)) < interval:
            continue
        info = account_switcher._describe(account_switcher.source_path(filename))
        env = account_switcher._read_env(account_switcher.source_path(filename))
        if info["provider"] != "openai" and not account_switcher._zai_host(env):
            continue  # Anthropic only exposes the CLI cache, not a live collector here.
        _attempts[filename] = now
        try:
            sample = message_costs.capture(filename)
            if sample["billing"] != "subscription" or not sample.get("account") or not sample["windows"]:
                continue
            period = account_usage_history.subscription_period(filename)
            if period:
                sample["subscriptionPeriod"] = period
            usage_history_store.record_account(state_dir, {**sample, "source": "background"})
            _remember(sample)
            hook_log.log("server", f"quota background: saved provider={sample['provider']} windows={len(sample['windows'])}")
        except Exception as exc:
            hook_log.log("server", f"quota background: collection failed ({type(exc).__name__})")


def run_monitor(state_dir, stop):
    hook_log.log("server", "quota background: monitor started")
    while not stop.is_set():
        try:
            poll(state_dir)
        except Exception as exc:
            hook_log.log("server", f"quota background: scan failed ({type(exc).__name__})")
        stop.wait(30)
