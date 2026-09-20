"""Repair Usage-history measurements from local Codex rollouts, with a backup."""
import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def rows(path):
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                yield json.loads(line)
            except ValueError:
                continue  # A live writer may not have finished the last line.


def measured_compactions(path):
    """Pair each compaction with adjacent real model input-token counts."""
    before = None
    pending = None
    for row in rows(path):
        payload = row.get("payload") or {}
        if row.get("type") == "compacted":
            pending = {"ts": row["timestamp"], "context_before": before}
            before = None
        elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
            usage = (payload.get("info") or {}).get("last_token_usage") or {}
            count = usage.get("input_tokens", 0)
            if count > 0:
                if pending:
                    yield {**pending, "context_after": count}
                    pending = None
                before = count


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def repairs(runtime, sessions):
    runtime, sessions = Path(runtime), Path(sessions)
    audit = {r["event_id"]: r for r in rows(runtime / "codex-compactions.jsonl")}
    existing = {r["event_id"]: r for r in rows(runtime / "codex-context-events.jsonl")}
    by_thread = {}
    for event in audit.values():
        if event.get("phase") == "completed" and event.get("completed_at"):
            by_thread.setdefault(event["thread_id"], []).append(event)
    result = []
    for path in sessions.rglob("rollout-*.jsonl"):
        thread_id = path.stem[-36:]
        candidates = by_thread.get(thread_id, [])
        if not candidates:
            continue
        used = set()
        for measured in measured_compactions(path):
            matches = [e for e in candidates if e["event_id"] not in used
                       and abs(timestamp(e["completed_at"]) - timestamp(measured["ts"])) < 2]
            if len(matches) != 1:
                continue  # Never guess when the audit cannot identify an event.
            event = matches[0]
            used.add(event["event_id"])
            old = existing.get(event["event_id"])
            if old is None:
                legacy = existing.get("turn:" + str(event.get("turn_id")))
                if (legacy and legacy.get("session_hash") == event.get("session_hash")
                        and abs(timestamp(legacy["ts"]) - timestamp(measured["ts"])) < 2):
                    old = legacy
            corrected = {
                "kind": "compact", "event_id": old["event_id"] if old else event["event_id"],
                "thread_id": thread_id, "turn_id": event.get("turn_id"),
                "ts": old["ts"] if old else event["completed_at"],
                "session_hash": event["session_hash"], "model": event["model"],
                "context_window": event.get("context_window"),
                "context_before": measured["context_before"],
                "context_after": measured["context_after"],
                "measurement": "adjacent model input usage from Codex rollout",
            }
            if old is None or any(old.get(k) != corrected[k] for k in ("context_before", "context_after")):
                result.append(corrected)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=Path(__file__).resolve().parent.parent / "hooks-runtime")
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    updates = repairs(args.runtime, args.sessions)
    backup = None
    if args.apply and updates:
        path = args.runtime / "codex-context-events.jsonl"
        backup = path.with_name("codex-context-events.before-repair-"
                                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".jsonl")
        shutil.copy2(path, backup)
        with path.open("a", encoding="utf-8") as handle:
            for update in updates:
                handle.write(json.dumps(update, ensure_ascii=False) + "\n")
    print(json.dumps({"updates": len(updates), "applied": args.apply,
                      "backup": str(backup) if backup else None}))


if __name__ == "__main__":
    main()
