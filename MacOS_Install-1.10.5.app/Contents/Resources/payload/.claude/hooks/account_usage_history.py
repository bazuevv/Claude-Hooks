"""Local account history: observed quota changes and transcript cost estimates.

No billing/admin API, credentials or message text are returned to the webview.
Quota samples are merged across sessions before calculating deltas, so concurrent
tasks cannot multiply the same account-wide usage. Missing intervals stay unknown.
"""
import hashlib
import json
import os
import calendar
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import account_switcher
import message_costs
import message_timestamps
import usage_history_store
import quota_periods


def _consume(data, event):
    # Retain quota observations, but only the latest start per API message.
    if event.get("billing") == "subscription":
        period = event.get("subscriptionPeriod")
        if isinstance(period, dict) and isinstance(period.get("start"), str) and isinstance(period.get("end"), str):
            data[("period", event.get("account"), period["start"], period["end"])] = {
                "account_file": event.get("account_file"), "account": event.get("account"), "subscriptionPeriod": period}
        for window in event.get("windows", []):
            if not isinstance(window, dict):
                continue
            if not all(message_costs.number(window.get(k)) for k in ("observed_at", "reset_at", "used")):
                continue
            key = (event.get("account"), window.get("key"), window["observed_at"],
                   window["reset_at"], window["used"])
            data[key] = {"account_file": event.get("account_file"), "account": event.get("account"), **window,
                         **{k: event.get(k) for k in ("source", "task_id", "phase", "task_started_at")}}
    elif event.get("billing") == "api" and event.get("phase") == "start":
        data[("api", event.get("id"))] = event


def quota_days(samples, *, hourly=False):
    """Attribute measured increases to detection time, without inventing hourly splits.

    Green requires a Claude Code task observation with its own baseline.
    Provider-wide counters cannot separate simultaneous use by another client.
    """
    days, previous = {}, {}
    unique = {(s.get("account"), s.get("key"), s["observed_at"], s["reset_at"], s["used"]): s for s in samples}
    for sample in sorted(unique.values(), key=lambda s: s["observed_at"]):
        stamp = sample["observed_at"]
        if stamp <= 0 or not 0 <= sample["used"] <= 100 or stamp >= sample["reset_at"]:
            continue
        moment = datetime.fromtimestamp(stamp)
        day = moment.strftime("%Y-%m-%dT%H") if hourly else moment.date().isoformat()
        series = (sample.get("account"), sample.get("key"), sample.get("label"))
        row = days.setdefault(day, {"date": day, "windows": {}})
        window = row["windows"].setdefault(series, {
            "key": sample.get("key"), "label": sample.get("label") or sample.get("key"), "percent": None,
            "first": sample["used"], "last": sample["used"], "samples": 0, "gaps": False,
            "externalPercent": 0,
            "localPercent": 0,
        })
        window["last"] = sample["used"]
        window["samples"] += 1
        before = previous.get(series)
        seconds = quota_periods.duration(sample)
        bucket_start = moment.replace(minute=0, second=0, microsecond=0) if hourly else moment.replace(hour=0, minute=0, second=0, microsecond=0)
        contained = seconds and sample["reset_at"] - seconds >= bucket_start.timestamp() - 2
        delta = None
        since = before["observed_at"] if before else sample["reset_at"] - seconds if seconds else stamp
        if before:
            same = abs(before["reset_at"] - sample["reset_at"]) <= 2 or before["reset_at"] > stamp
            if same and sample["used"] >= before["used"]:
                delta = sample["used"] - before["used"]
            elif (not same and sample["reset_at"] > before["reset_at"] and seconds
                  and sample["reset_at"] - seconds >= before["observed_at"] - 2):
                delta = sample["used"]
                since = sample["reset_at"] - seconds
                window["gaps"] = True
            else:
                window["gaps"] = True
                if same:
                    # An out-of-order/adjusted counter must not count its rebound twice.
                    continue
        elif contained:
            delta = sample["used"]
        if delta is not None:
            window["percent"] = round((window["percent"] or 0) + delta, 4)
            if delta > 0:
                baseline = sample.get("task_started_at")
                local = (sample.get("source") == "claude-code" and bool(sample.get("task_id"))
                         and sample.get("phase") in ("progress", "end") and message_costs.number(baseline)
                         and since >= baseline - 2 and stamp >= baseline)
                if local:
                    window["localPercent"] = round(window["localPercent"] + delta, 4)
                if not local:
                    window["externalPercent"] = round(window["externalPercent"] + delta, 4)
                window["observedFrom"] = min(window.get("observedFrom", since), since)
                window["observedTo"] = stamp
        previous[series] = sample
    for row in days.values():
        row["windows"] = list(row["windows"].values())
    return days


def _save_sample(state_dir, sample):
    return usage_history_store.record_account(state_dir, sample)


def subscription_period(filename):
    """Keep only the known paid period; never extrapolate earlier renewals."""
    record = account_switcher.read_subscription(filename)
    info = account_switcher.subscription_info(record) if record else None
    env = account_switcher._read_env(account_switcher.source_path(filename))
    if account_switcher._zai_host(env):
        info = account_switcher.zai_subscription(env) or info
    if not info:
        return None
    try:
        start = datetime.fromisoformat(info["paidAt"]).astimezone()
        end = datetime.fromisoformat(info["until"]).astimezone()
        if end > start:
            return {"start": start.isoformat(), "end": end.isoformat()}
    except (KeyError, TypeError, ValueError):
        pass
    return None


def subscription_weeks(bucket, periods):
    hourly = "T" in bucket
    start = datetime.fromisoformat(bucket + (":00:00" if hourly else "T00:00:00")).astimezone()
    end = start + (timedelta(hours=1) if hourly else timedelta(days=1))
    weeks = {}
    for period in periods:
        try:
            paid = datetime.fromisoformat(period["start"])
            until = datetime.fromisoformat(period["end"])
            overlap = max(start, paid)
            if overlap >= min(end, until):
                continue
            number = (overlap - paid).days // 7 + 1
            week_start = paid + timedelta(weeks=number - 1)
            while week_start < min(end, until):
                week_end = min(week_start + timedelta(weeks=1), until)
                weeks[(week_start.isoformat(), number)] = {
                    "number": number, "start": week_start.isoformat(), "end": week_end.isoformat()}
                week_start += timedelta(weeks=1)
                number += 1
        except (KeyError, TypeError, ValueError):
            continue
    return [weeks[key] for key in sorted(weeks)]


def weekly_usage(samples, period, account, *, boundaries=None):
    """Observed totals per paid week, keeping different quota windows separate.

    A counter whose quota window started within the week can contribute its
    usage observed in that week, even if collection began mid-window. For windows
    starting earlier only measured increases inside the paid week are attributable.
    """
    if not period and boundaries:
        period = {"start": boundaries[0]["start"], "end": boundaries[-1]["end"]}
    if not period:
        return []
    paid = datetime.fromisoformat(period["start"])
    until = datetime.fromisoformat(period["end"])
    weeks = []
    while paid < until:
        end = min(paid + timedelta(weeks=1), until)
        weeks.append({"number": len(weeks) + 1, "start": paid.isoformat(), "end": end.isoformat(), "windows": []})
        paid = end
    if boundaries is not None:
        weeks = [{**week, "windows": []} for week in boundaries]
    ordered = sorted(samples, key=lambda s: s["observed_at"])
    for week in weeks:
        start = datetime.fromisoformat(week["start"]).timestamp()
        end = datetime.fromisoformat(week["end"]).timestamp()
        series = {}
        for sample in ordered:
            if (sample.get("account") != account or not start <= sample["observed_at"] < end
                    or sample["observed_at"] >= sample["reset_at"] or not 0 <= sample["used"] <= 100):
                continue
            key = (sample.get("key"), sample.get("label"))
            groups = series.setdefault(key, [])
            group = next((g for g in groups if abs(g[0]["reset_at"] - sample["reset_at"]) <= 2), None)
            if group is None:
                groups.append([sample])
            else:
                group.append(sample)
        for (key, label), groups in series.items():
            total = None
            for group in groups:
                first = group[0]
                maximum = max(s["used"] for s in group)
                seconds = quota_periods.duration(first)
                contained = seconds and first["reset_at"] - seconds >= start - 2
                if contained:
                    value = maximum
                elif len({s["observed_at"] for s in group}) > 1:
                    value = maximum - first["used"]
                else:
                    continue
                total = round((total or 0) + value, 4)
            week["windows"].append({"key": key, "label": label, "percent": total})
    return weeks


def provider_weeks(cycles, paid_period):
    """Actual provider boundaries override the subscription purchase calendar."""
    known = [p for p in cycles if p["duration"] == 7 * 86400]
    if not known:
        return []
    # Prefer the general weekly quota over a model-specific weekly quota.
    key = next((p["window"] for p in reversed(known) if p["window"] in ("primary", "secondary", "seven_day")), known[-1]["window"])
    known = [p for p in known if p["window"] == key]
    start = datetime.fromisoformat(paid_period["start"]).timestamp() if paid_period else known[-1]["start"]
    end = datetime.fromisoformat(paid_period["end"]).timestamp() if paid_period else known[-1]["end"]
    actual = [p for p in known if p["end"] > start and p["start"] < end]
    if not actual:
        return []
    intervals = [{"id": p["id"], "start": p["start"], "end": p["end"], "estimated": False} for p in actual]
    # Empty earlier/future slots are explicitly estimates, never archived as observed resets.
    cursor = actual[-1]["start"]
    while cursor - 7 * 86400 >= start:
        cursor -= 7 * 86400
    while cursor < end:
        until = cursor + 7 * 86400
        if not any(p["start"] < until and p["end"] > cursor for p in actual):
            intervals.append({"start": cursor, "end": until, "estimated": True})
        cursor = until
    return [{"id": p.get("id"), "number": i + 1, "start": datetime.fromtimestamp(p["start"]).astimezone().isoformat(),
             "end": datetime.fromtimestamp(p["end"]).astimezone().isoformat(), "estimated": p["estimated"]}
            for i, p in enumerate(sorted(intervals, key=lambda p: p["start"]))]


def provider_week_hints(bucket, cycles, paid_periods):
    start = datetime.fromisoformat(bucket + (":00:00" if "T" in bucket else "T00:00:00")).astimezone()
    end = start + (timedelta(hours=1) if "T" in bucket else timedelta(days=1))
    periods = [p for p in paid_periods if datetime.fromisoformat(p["start"]) < end and datetime.fromisoformat(p["end"]) > start]
    weeks = [w for period in (periods or [None]) for w in provider_weeks(cycles, period)
             if datetime.fromisoformat(w["start"]) < end and datetime.fromisoformat(w["end"]) > start]
    return list({(w["start"], w["end"]): w for w in weeks}.values())


def short_usage(cycles, paid_period):
    if not paid_period:
        return []
    start = datetime.fromisoformat(paid_period["start"]).timestamp()
    end = datetime.fromisoformat(paid_period["end"]).timestamp()
    return [{k: p[k] for k in ("id", "window", "label", "start", "end", "used")}
            for p in sorted(cycles, key=lambda p: p["start"])
            if p["duration"] == 5 * 3600 and p["end"] > start and p["start"] < end]


def _transcript_map():
    # Only top-level sessions; subagents are accounted for by message_costs.
    result = {}
    for path in (Path(account_switcher.CLAUDE_DIR) / "projects").glob("*/*.jsonl"):
        key = hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()
        result[key] = str(path)
    return result


def selected_period(mode="day", selected=None):
    today = date.today()
    selected = selected or (today.isoformat() if mode == "day" else today.strftime("%Y-%m"))
    if mode not in ("day", "month") or not re.fullmatch(r"\d{4}-\d{2}" + (r"-\d{2}" if mode == "day" else ""), selected):
        raise ValueError("Неверный период истории")
    start = date.fromisoformat(selected if mode == "day" else selected + "-01")
    if mode == "day":
        keys = [selected + f"T{hour:02d}" for hour in range(24)]
    else:
        keys = [selected + f"-{day:02d}" for day in range(1, calendar.monthrange(start.year, start.month)[1] + 1)]
    return {"mode": mode, "date": selected}, keys


def add_api(row, cost):
    api = row.setdefault("api", {"usd": 0, "messages": 0, "unknown": 0, "requests": 0})
    api["messages"] += 1
    api["requests"] += cost.get("requests", 0)
    if message_costs.number(cost.get("usd")):
        api["usd"] = round(api["usd"] + cost["usd"], 8)
    else:
        api["unknown"] += 1


def details(filename, state_dir, *, mode="day", selected=None):
    period, bucket_keys = selected_period(mode, selected)
    ok, error, _ = account_switcher._load_account(filename)
    if not ok:
        raise ValueError(error)
    sample = message_costs.capture(filename)
    if sample["billing"] == "subscription":
        paid_period = subscription_period(filename)
        if paid_period:
            sample = {**sample, "subscriptionPeriod": paid_period}
    sample_path = _save_sample(state_dir, {**sample, "source": "accs"})
    identities = quota_periods.account_ids(sample_path.parent, sample.get("account"), sample.get("legacy_accounts", []))
    cycles = quota_periods.cycles(sample_path.parent, sample.get("account"), sample.get("legacy_accounts", []))
    api_logs = []
    periods = quota_periods.subscription_periods(sample_path.parent, sample.get("account"))
    # Older/account-settings dates can omit the local timezone.
    periods = [dict(zip(("start", "end"), pair)) for pair in sorted({
        tuple(datetime.fromisoformat(p[key]).astimezone().isoformat() for key in ("start", "end"))
        for p in periods})]
    for path in sample_path.parent.glob("message-costs-*.jsonl"):
        for event in message_costs._read_index(str(path), _consume, cache_tag="account-history").values():
            if event.get("account") not in identities:
                continue
            if event.get("billing") == "api":
                api_logs.append((path, event))
    current_period = sample.get("subscriptionPeriod")
    if not current_period:
        current_period = next((p for p in reversed(periods) if
            datetime.fromisoformat(p["start"]).timestamp() <= sample["captured_at"] < datetime.fromisoformat(p["end"]).timestamp()), None)
    cycle_samples = [{**s, "account": sample.get("account"), "key": p["window"], "label": p["label"],
                      "reset_at": p["end"], "duration_seconds": p["duration"]} for p in cycles for s in p["samples"]]
    days = quota_days(cycle_samples)
    hours = quota_days(cycle_samples, hourly=True) if mode == "day" else {}
    # Resolve older journals that predate the stored transcript path lazily.
    paths, legacy, missing_journals = {}, None, set()
    for journal, event in api_logs:
        path = event.get("transcript")
        if not path:
            if legacy is None:
                legacy = _transcript_map()
            path = legacy.get(journal.stem.removeprefix("message-costs-"))
        if path:
            paths[path] = True
        else:
            missing_journals.add(str(journal))
    missing_logs = len(missing_journals)
    for path in paths:
        try:
            snapshot = message_timestamps.snapshot(path, state_dir)
            saved = message_costs.observations(path, state_dir)
        except (OSError, ValueError):
            missing_logs += 1
            continue
        for message_id, task in snapshot["tasks"].items():
            start = saved.get(message_id, {}).get("start", {})
            if start.get("account") not in identities or start.get("billing") != "api":
                continue
            cost = task.get("cost", {})
            sent = datetime.fromisoformat(snapshot["times"][message_id].replace("Z", "+00:00")).astimezone()
            day = sent.date().isoformat()
            row = days.setdefault(day, {"date": day, "windows": []})
            add_api(row, cost)
            if mode == "day":
                hour = sent.strftime("%Y-%m-%dT%H")
                add_api(hours.setdefault(hour, {"date": hour, "windows": []}), cost)
    # A fixed recent calendar distinguishes a missing day from zero usage.
    today = datetime.now().date()
    recent = []
    for offset in range(30):
        day = (today - timedelta(days=offset)).isoformat()
        recent.append(days.get(day, {"date": day, "windows": [], "missing": True}))
    buckets = []
    for key in bucket_keys:
        row = dict((hours if mode == "day" else days).get(key, {"date": key, "windows": [], "missing": True}))
        if sample["billing"] == "subscription" or row.get("windows"):
            row["subscriptionWeeks"] = provider_week_hints(key, cycles, periods)
        buckets.append(row)
    return {"ok": True, "file": filename, "billing": sample["billing"],
            "windows": sample["windows"], "days": recent, "missingLogs": missing_logs,
            "period": period, "buckets": buckets,
            "subscriptionPeriod": current_period,
            "shortUsage": short_usage(cycles, current_period) if sample["billing"] == "subscription" else [],
            "quotaPeriods": [{k: v for k, v in cycle.items() if k not in ("samples", "account")} for cycle in cycles],
            "weeklyUsage": weekly_usage(cycle_samples, current_period, sample.get("account"),
                                        boundaries=provider_weeks(cycles, current_period))
                if sample["billing"] == "subscription" else [],
            "updatedAt": sample["captured_at"], "approximate": True}
