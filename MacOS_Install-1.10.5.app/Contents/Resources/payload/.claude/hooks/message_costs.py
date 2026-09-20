"""Per-message billing observations. Never equate subscription tokens to money.

Submit/stop hooks capture limits; active chats schedule throttled background samples.
Limit deltas are account-wide observations, not per-request billing records.
"""
import hashlib
import json
import math
import os
import threading
import time
import tomllib
import uuid
from datetime import datetime
from collections import OrderedDict
from pathlib import Path

import account_switcher
import usage_history_store

CONFIG_PATH = Path(__file__).resolve().parent.parent / "message-costs.toml"
HOOK_CONFIG_PATH = Path(__file__).resolve().parent.parent / "patches" / "claude-custom-config.toml"
_LOCK = threading.RLock()
_FILES = OrderedDict()
_REFRESHING = set()
_RETRY_AT = {}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_config(path):
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, ValueError):
        return {}


def config():
    rates = _read_config(CONFIG_PATH)
    hooks = _read_config(HOOK_CONFIG_PATH)
    accounts = {name: {"api_rates": value.get("api_rates", {})}
                for name, value in rates.get("accounts", {}).items() if isinstance(value, dict)}
    for name, value in hooks.get("messageCostAccounts", {}).items():
        if isinstance(value, dict):
            accounts.setdefault(name, {}).update({key: value[key] for key in ("billing", "history_id") if key in value})
    return {"enabled": hooks.get("messageCostsEnabled", True),
            "refresh_interval_seconds": hooks.get("messageCostsRefreshIntervalSec", 30),
            "accounts": accounts, "api_rates": rates.get("api_rates", {})}


def refresh_interval():
    value = config().get("refresh_interval_seconds", 30)
    if not number(value) or value < 0:
        return 30
    return max(1, value) if value else 0


def events_path(transcript, state_dir):
    key = hashlib.sha256(os.path.normcase(os.path.abspath(transcript)).encode()).hexdigest()
    return str(usage_history_store.prepare(state_dir) / ("message-costs-" + key + ".jsonl"))


def _read_index(path, consume, *, cache_tag=None):
    """Cache complete JSONL records, including corrections to streamed usage."""
    with _LOCK:
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return {}
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cache_key = (path, cache_tag)
        entry = _FILES.get(cache_key)
        if entry and entry["signature"] == signature:
            return dict(entry["data"])
        if not entry or signature[0] != entry["signature"][0] or stat.st_size <= entry["offset"]:
            entry = {"offset": 0, "data": {}}
        with open(path, "rb") as handle:
            handle.seek(entry["offset"])
            while True:
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break
                entry["offset"] = handle.tell()
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        consume(entry["data"], value)
                except (ValueError, TypeError, KeyError):
                    continue
        entry["signature"] = signature
        _FILES[cache_key] = entry
        _FILES.move_to_end(cache_key)
        while len(_FILES) > 64:
            _FILES.popitem(last=False)
        return dict(entry["data"])


def observations(transcript, state_dir):
    def consume(data, event):
        if isinstance(event.get("id"), str) and event.get("phase") in ("start", "progress", "end"):
            data.setdefault(event["id"], {})[event["phase"]] = event
    with _LOCK:
        return {key: dict(value) for key, value in _read_index(events_path(transcript, state_dir), consume).items()}


def append_event(transcript, state_dir, event):
    event = {**event, "transcript": os.path.abspath(transcript)}
    start = None
    if event.get("billing") == "subscription" and event.get("phase") in ("progress", "end"):
        start = observations(transcript, state_dir).get(event.get("id"), {}).get("start")
    event = usage_history_store.quota_periods.task_source(event, start)
    payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    usage_history_store.append(state_dir, Path(events_path(transcript, state_dir)).name, payload)


def _window(key, label, used, reset, observed):
    if not number(used) or not 0 <= used <= 100 or not number(observed) or (reset is not None and not number(reset)):
        return None
    result = {"key": key, "label": label, "used": used, "reset_at": reset, "observed_at": observed}
    result["duration_seconds"] = usage_history_store.quota_periods.duration(result)
    return result


def capture(filename=None, *, openai_snapshot=None, zai_sample=None):
    """Capture only billing metadata, never credentials or account emails."""
    now = time.time()
    cfg = config()
    active = filename is None
    filename = filename or account_switcher.get_current_account()
    override = cfg.get("accounts", {}).get(filename, {})
    path = account_switcher.SETTINGS_FILE if active else account_switcher.source_path(filename)
    env = account_switcher._read_env(path)
    info = account_switcher._describe(path)
    provider = info["provider"]
    identity = "|".join(str(env.get(k) or "") for k in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"))
    canonical_identity = identity
    mode = "api"
    windows = []
    if provider == "openai":
        mode = "subscription"
        snap = openai_snapshot if openai_snapshot is not None else account_switcher.codex_bridge_manager.account_snapshot(timeout=4.0) or {}
        now = time.time()
        account = snap.get("account") or {}
        identity = json.dumps(account, sort_keys=True)
        canonical_identity = account.get("accountId") or account.get("id") or account.get("email")
        if account.get("type") == "apiKey":
            mode = "api"
        limits = snap.get("rateLimits") or {}
        for key, label in (("primary", "5 ч"), ("secondary", "7 дн")):
            value = limits.get(key) or {}
            minutes = value.get("windowDurationMins")
            if number(minutes) and minutes > 0:
                label = f"{minutes / 60:g} ч" if minutes < 1440 else f"{minutes / 1440:g} дн"
            windows.append(_window(key, label, value.get("usedPercent"), value.get("resetsAt"), now))
    elif info["oauth"]:
        mode = "subscription"
        cached = account_switcher._read_usage_cache() or {}
        identity = str(cached.get("accountUuid") or "unknown")
        canonical_identity = cached.get("accountUuid")
        observed = cached.get("fetchedAtMs")
        observed = observed / 1000 if number(observed) else None
        for key, label, _ in account_switcher.USAGE_WINDOWS:
            value = (cached.get("utilization") or {}).get(key) or {}
            windows.append(_window(key, label, value.get("utilization"),
                                   account_switcher._reset_moment(value.get("resets_at")), observed))
            if windows[-1] and key.startswith("seven_day"):
                windows[-1]["duration_seconds"] = 7 * 86400
    elif account_switcher._zai_host(env):
        provider = "zai"
        # Coding Plan has a separate endpoint from the pay-as-you-go API.
        if "/coding/" in str(env.get("ANTHROPIC_BASE_URL", "")) or override.get("billing") == "subscription":
            mode = "subscription"
            value = zai_sample if zai_sample is not None else account_switcher.zai_usage(env, force_refresh=True) or {}
            now = time.time()
            age = value.get("ageSec") or 0
            observed = value.get("observedAt")
            observed = observed if number(observed) else now - age
            for window in value.get("windows", []):
                left = window.get("resetsInSec")
                reset = window.get("resetAt")
                reset = reset if number(reset) else observed + left if number(left) else None
                captured = _window(window.get("key"), window.get("label"), window.get("percent"),
                                   reset, observed)
                if captured:
                    captured["expired"] = bool(window.get("expired"))
                windows.append(captured)
    if override.get("billing") in ("api", "subscription"):
        mode = override["billing"]
    # The bridge can return only {type: apiKey}; that is not an account ID.
    # An explicit non-secret ID lets such profiles opt into a stable archive.
    if override.get("history_id"):
        canonical_identity = "configured:" + str(override["history_id"])
    return {"billing": mode, "provider": provider, "account_file": filename,
            "account": hashlib.sha256((provider + "|" + str(canonical_identity)).encode()).hexdigest() if canonical_identity else None,
            "legacy_accounts": [hashlib.sha256((filename + "|" + identity).encode()).hexdigest()] if canonical_identity and identity not in ('{}', '{"type": "apiKey"}', 'unknown') else [],
            "captured_at": now, "windows": [w for w in windows if w],
            "rates": {**cfg.get("api_rates", {}), **override.get("api_rates", {})} if mode == "api" else {}}


def record_start(transcript, state_dir, message_id):
    if not config().get("enabled", True):
        return
    if observations(transcript, state_dir).get(message_id, {}).get("start"):
        return  # A repeated hook must not move the baseline past the first request.
    sample = capture()
    append_event(transcript, state_dir, {"id": message_id, "phase": "start", **sample})


def record_submission(transcript, state_dir, prompt):
    """The submit hook may run before Claude appends the user transcript row."""
    if not isinstance(prompt, str) or not config().get("enabled", True):
        return
    submitted_at = time.time()
    sample = capture()
    append_event(transcript, state_dir, {
        "id": "submit:" + uuid.uuid4().hex, "phase": "start", **sample,
        "submitted_at": submitted_at, "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
    })


def bind_submissions(transcript, state_dir, saved, messages, times):
    bound = {item["start"].get("submission_id") for item in saved.values() if item.get("start")}
    candidates = {}
    for key, message in messages.items():
        if saved.get(key, {}).get("start"):
            continue
        digest = hashlib.sha256(message["text"].encode()).hexdigest()
        sent = datetime.fromisoformat(times[key].replace("Z", "+00:00")).timestamp()
        candidates.setdefault(digest, []).append((sent, key))
    for submission, value in list(saved.items()):
        start = value.get("start", {})
        if not submission.startswith("submit:") or submission in bound:
            continue
        options = candidates.get(start.get("prompt_hash"), [])
        options = [(abs(sent - start["submitted_at"]), key) for sent, key in options
                   if abs(sent - start["submitted_at"]) <= 60 and not saved.get(key, {}).get("start")]
        if not options:
            continue
        _, key = min(options)
        event = {**start, "id": key, "submission_id": submission}
        append_event(transcript, state_dir, event)
        saved.setdefault(key, {})["start"] = event


def record_end(transcript, state_dir, message_ids, *, force=False):
    starts = observations(transcript, state_dir)
    pending = [i for i in message_ids if starts.get(i, {}).get("start", {}).get("billing") == "subscription"
               and (force or not starts[i].get("end"))]
    if not pending:
        return
    sample = capture()
    for message_id in pending:
        append_event(transcript, state_dir, {"id": message_id, "phase": "end", **sample})


def refresh_progress(transcript, state_dir, tasks, active_ids):
    """Schedule at most one quota request per session without blocking the UI."""
    interval = refresh_interval()
    if not interval:
        return
    path = events_path(transcript, state_dir)
    now = time.time()
    with _LOCK:
        if path in _REFRESHING or now < _RETRY_AT.get(path, 0):
            return
        saved = observations(transcript, state_dir)
        pending = []
        for key in active_ids:
            observation = saved.get(key, {})
            start = observation.get("start", {})
            latest = observation.get("progress") or start
            if (start.get("billing") == "subscription" and not observation.get("end")
                    and tasks.get(key, {}).get("source") not in ("stop_hook", "ui_stop", "interrupted")
                    and now - latest.get("captured_at", now) >= interval):
                pending.append(key)
        if not pending:
            return
        _REFRESHING.add(path)

    def update():
        try:
            sample = capture()
            # Stop can finish while the provider request is in flight. Never
            # overwrite a final observation or revive a stopped task.
            saved = observations(transcript, state_dir)
            for key in pending:
                if not saved.get(key, {}).get("end"):
                    append_event(transcript, state_dir, {"id": key, "phase": "progress", **sample})
        except (OSError, ValueError, TypeError):
            with _LOCK:
                _RETRY_AT[path] = time.time() + interval
            import hook_log
            hook_log.log("message-costs", "intermediate observation failed")
        finally:
            with _LOCK:
                _REFRESHING.discard(path)
    threading.Thread(target=update, name="message-cost-refresh", daemon=True).start()


def add_request(requests, record, owner):
    message = record.get("message")
    if not owner or not isinstance(message, dict) or record.get("type") != "assistant":
        return
    model, usage = message.get("model"), message.get("usage")
    key = message.get("id") or record.get("uuid")
    if not isinstance(key, str) or not isinstance(model, str) or model == "<synthetic>" or not isinstance(usage, dict):
        return
    clean = {}
    for name in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(name, 0)
        if not number(value) or value < 0:
            return
        clean[name] = value
    for name in ("cache_creation", "server_tool_use"):
        nested = usage.get(name)
        clean[name] = {k: v for k, v in nested.items() if number(v) and v >= 0} if isinstance(nested, dict) else {}
    clean["speed"] = usage.get("speed")
    clean["inference_geo"] = usage.get("inference_geo")
    clean["service_tier"] = usage.get("service_tier")
    previous = requests.get(key)
    if previous:
        # A streamed answer may be written once per content block, then updated.
        # Count the request once, keeping its final counters rather than summing.
        for name, value in clean.items():
            if number(value):
                clean[name] = max(value, previous["usage"].get(name, 0))
        owner = previous["owner"]
    requests[key] = {"owner": owner, "model": model, "usage": clean}


def api_cost(requests, rates):
    total, missing = 0.0, False
    for request in requests:
        model, usage = request["model"], request["usage"]
        matches = [key for key in rates if model == key or model.startswith(key + "-")]
        rate = rates[max(matches, key=len)] if matches else None
        if not isinstance(rate, dict) or not all(number(rate.get(k)) and rate[k] >= 0 for k in ("input", "output", "cache_read")):
            missing = True
            continue
        created = usage["cache_creation_input_tokens"]
        cache = usage.get("cache_creation", {})
        hour = min(created, cache.get("ephemeral_1h_input_tokens", 0))
        short = min(created - hour, cache.get("ephemeral_5m_input_tokens", 0))
        if created != hour + short or (hour and not number(rate.get("cache_write_1h"))) or (short and not number(rate.get("cache_write_5m"))):
            missing = True
            continue
        cost = (usage["input_tokens"] * rate["input"] + usage["output_tokens"] * rate["output"]
                + usage["cache_read_input_tokens"] * rate["cache_read"]
                + hour * rate.get("cache_write_1h", 0) + short * rate.get("cache_write_5m", 0)) / 1e6
        if usage.get("service_tier") not in (None, "standard"):
            missing = True
            continue
        if usage.get("speed") == "fast":
            if not number(rate.get("fast_multiplier")):
                missing = True
                continue
            cost *= rate["fast_multiplier"]
        if usage.get("inference_geo") == "us":
            cost *= 1.1
        searches = usage.get("server_tool_use", {}).get("web_search_requests", 0)
        if searches and not number(rate.get("web_search")):
            missing = True
            continue
        total += cost + searches * rate.get("web_search", 0)
    return {"kind": "api", "usd": round(total, 8) if requests and not missing else None,
            "reason": "unknown_rate" if missing else "no_usage" if not requests else "",
            "requests": len(requests), "approximate": True}


def subscription_cost(start, end):
    if not end:
        return {"kind": "subscription", "reason": "pending", "windows": []}
    identities_before = {v for v in (start["account"], *start.get("legacy_accounts", [])) if v}
    identities_after = {v for v in (end["account"], *end.get("legacy_accounts", [])) if v}
    if not identities_before.intersection(identities_after) or start["billing"] != end["billing"]:
        return {"kind": "subscription", "reason": "account_changed", "windows": []}
    windows = []
    reason = "no_limits"
    after = {w["key"]: w for w in end.get("windows", [])}
    for before in start.get("windows", []):
        if (start.get("provider") == "zai" or end.get("provider") == "zai") and before.get("key") != "five_hour":
            continue
        value = after.get(before["key"])
        if not value:
            continue
        if not number(before.get("reset_at")) or not number(value.get("reset_at")):
            # A known quota remains selectable even when its reset is unknown,
            # but its per-message consumption cannot be attributed safely.
            reason = "no_limits"
            continue
        if abs(value["reset_at"] - before["reset_at"]) > 2 or value["used"] < before["used"] or value["reset_at"] <= end["captured_at"]:
            reason = "reset"
            continue
        if (start["captured_at"] - before["observed_at"] > 60
                or end["captured_at"] - value["observed_at"] > 15
                or value["observed_at"] <= before["observed_at"]):
            reason = "stale"
            continue
        windows.append({"label": before["label"], "percent": round(value["used"] - before["used"], 4)})
    return {"kind": "subscription", "windows": windows, "approximate": True,
            "reason": "" if windows else reason}


def attach(transcript, state_dir, tasks, requests, agents, messages, times, running_ids=()):
    """Join saved billing mode with usage attributed to each user message."""
    saved = observations(transcript, state_dir)
    bind_submissions(transcript, state_dir, saved, messages, times)
    interval = refresh_interval()
    api_owners = {key for key, value in saved.items() if value.get("start", {}).get("billing") == "api"}
    grouped = {}
    for request in requests.values():
        if request["owner"] in api_owners:
            grouped.setdefault(request["owner"], []).append(request)
    for agent, owner in agents.items():
        if owner not in api_owners or not isinstance(agent, str) or not all(c.isalnum() or c in "-_" for c in agent):
            continue
        path = str(Path(transcript).with_suffix("") / "subagents" / ("agent-" + agent + ".jsonl"))
        child = _read_index(path, lambda data, rec: add_request(data, rec, owner))
        grouped.setdefault(owner, []).extend(child.values())
    for message_id, task in tasks.items():
        task.pop("cost_refresh", None)
        observations_for_message = saved.get(message_id, {})
        task.pop("account_usage", None)
        latest_sample = (observations_for_message.get("end") or observations_for_message.get("progress")
                         or observations_for_message.get("start") or {})
        if latest_sample.get("billing") == "subscription" and latest_sample.get("account_file") and latest_sample.get("windows"):
            windows = latest_sample["windows"]
            observed = min(w["observed_at"] for w in windows)
            provider = latest_sample.get("provider")
            source = {"zai": "Данные Z.AI", "openai": "Данные Codex App Server",
                      "anthropic": "Данные Claude Code"}.get(provider, "Данные провайдера")
            task["account_usage"] = {"file": latest_sample["account_file"], "usage": {
                "observedAt": observed, "ageSec": max(0, time.time() - observed), "sourceLabel": source,
                "windows": [{"key": "openai_" + w["key"] if provider == "openai" else w["key"],
                             "label": w["label"], "percent": w["used"],
                             "resetAt": w.get("reset_at"), "expired": w.get("expired", False),
                             "resetsInSec": max(0, w["reset_at"] - time.time()) if number(w.get("reset_at")) else None}
                            for w in windows],
            }}
        start = observations_for_message.get("start")
        if not start:
            task["cost"] = {"kind": "unknown", "reason": "no_baseline"}
        elif start["billing"] == "api":
            progress = observations_for_message.get("progress", {})
            running = message_id in running_ids and task.get("source") not in ("stop_hook", "ui_stop", "interrupted")
            if (running and "cost" in progress
                    and (not interval or time.time() - progress["captured_at"] < interval)):
                task["cost"] = progress["cost"]
            else:
                task["cost"] = api_cost(grouped.get(message_id, []), start.get("rates", {}))
                if running:
                    append_event(transcript, state_dir, {"id": message_id, "phase": "progress",
                                 "captured_at": time.time(), "cost": task["cost"]})
        else:
            task["cost"] = subscription_cost(start, observations_for_message.get("end")
                                             or observations_for_message.get("progress"))
            if (interval and message_id in running_ids and not observations_for_message.get("end")
                    and task.get("source") not in ("stop_hook", "ui_stop", "interrupted")):
                latest = observations_for_message.get("progress") or start
                path = events_path(transcript, state_dir)
                with _LOCK:
                    task["cost_refresh"] = {
                        "next_at": max(latest["captured_at"] + interval, _RETRY_AT.get(path, 0)),
                        "updating": path in _REFRESHING,
                    }
