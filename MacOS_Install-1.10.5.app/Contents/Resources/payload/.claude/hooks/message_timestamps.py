"""Incremental user-message timing index with opt-in chat history text."""

import json
import hashlib
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone

import message_costs

_LOCK = threading.RLock()
_CACHE = OrderedDict()
_MAX_SESSIONS = 8


def _index(transcript_path: str) -> dict:
    path = os.path.abspath(transcript_path)
    with _LOCK:
        stat = os.stat(path)
        entry = _CACHE.get(path)
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if entry and entry["signature"] == signature:
            return entry
        if (entry is None or entry["signature"][0] != stat.st_ino
                or stat.st_size <= entry["signature"][2]):
            entry = {"offset": 0, "times": {}, "tasks": {}, "messages": {}, "active_ids": [],
                     "requests": {}, "agents": {}, "assistant_owners": {}}
        with open(path, "rb") as handle:
            handle.seek(entry["offset"])
            while True:
                line = handle.readline()
                if not line or not line.endswith(b"\n"):
                    break  # A partially written record will be retried next time.
                entry["offset"] = handle.tell()
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict) or record.get("isSidechain"):
                        continue
                    timestamp = record.get("timestamp")
                    if not isinstance(timestamp, str):
                        continue
                    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        message = {}
                    content = message.get("content") if isinstance(message, dict) else None
                    owner = entry["active_ids"][-1] if entry["active_ids"] else None
                    tool_result = record.get("toolUseResult")
                    if isinstance(tool_result, dict) and isinstance(tool_result.get("agentId"), str):
                        agent_owner = entry["assistant_owners"].get(record.get("sourceToolAssistantUUID"), owner)
                        if agent_owner:
                            entry["agents"].setdefault(tool_result["agentId"], agent_owner)
                    if record.get("type") == "assistant":
                        if record.get("parent_tool_use_id"):
                            continue
                        message_costs.add_request(entry["requests"], record, owner)
                        if owner and isinstance(record.get("uuid"), str):
                            entry["assistant_owners"][record["uuid"]] = owner
                        terminal = message.get("stop_reason") in ("end_turn", "stop_sequence", "max_tokens", "refusal")
                        for active_id in entry["active_ids"]:
                            entry["tasks"][active_id] = ({"completed_at": timestamp, "source": "transcript"}
                                                         if terminal else {})
                        continue
                    if record.get("type") != "user":
                        continue
                    if isinstance(content, list) and content and all(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in content
                    ):
                        continue  # Tool replies use role=user but are not user messages.
                    message_id = record.get("uuid")
                    if not isinstance(message_id, str) or not message_id:
                        continue
                except (ValueError, UnicodeDecodeError):
                    continue
                entry["times"].setdefault(message_id, timestamp)
                if record.get("isMeta") or record.get("isSynthetic"):
                    continue
                texts = ([content] if isinstance(content, str) else
                         [b.get("text", "") for b in content or [] if isinstance(b, dict)])
                if any(t.startswith("[Request interrupted by user") for t in texts):
                    for active_id in entry["active_ids"]:
                        entry["tasks"][active_id] = {"completed_at": timestamp, "source": "interrupted"}
                    continue
                if message_id in entry["tasks"]:
                    continue
                if any(entry["tasks"].get(i, {}).get("completed_at") for i in entry["active_ids"]):
                    entry["active_ids"] = []
                entry["active_ids"].append(message_id)
                entry["tasks"][message_id] = {}
                entry["messages"][message_id] = {
                    "text": "\n".join(t for t in texts if isinstance(t, str) and t),
                    "attachments": sum(1 for b in content if isinstance(b, dict)
                                       and b.get("type") in ("image", "document")) if isinstance(content, list) else 0,
                }
        entry["signature"] = signature
        _CACHE[path] = entry
        _CACHE.move_to_end(path)
        while len(_CACHE) > _MAX_SESSIONS:
            _CACHE.popitem(last=False)
        return entry


def collect(transcript_path: str) -> dict[str, str]:
    return dict(_index(transcript_path)["times"])


def _events_path(transcript_path: str, state_dir: str) -> str:
    key = hashlib.sha256(os.path.normcase(os.path.abspath(transcript_path)).encode()).hexdigest()
    return os.path.join(state_dir, "message-task-times-" + key + ".jsonl")


def _merge_completion(task, event):
    source = event["source"]
    completed = event["completed_at"]
    moment = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    if source in ("stop_hook", "ui_stop"):
        key = "server_completed_at" if source == "stop_hook" else "ui_completed_at"
        previous = task.get(key)
        if not previous or moment > datetime.fromisoformat(previous.replace("Z", "+00:00")):
            task[key] = completed
        candidates = [(task[key], kind) for key, kind in (
            ("server_completed_at", "stop_hook"), ("ui_completed_at", "ui_stop")) if task.get(key)]
        completed, source = max(candidates, key=lambda item: datetime.fromisoformat(item[0].replace("Z", "+00:00")))
    task.update(completed_at=completed, source=source)


def snapshot(transcript_path: str, state_dir: str | None = None, *, include_messages: bool = False,
             running: bool = False) -> dict:
    with _LOCK:
        entry = _index(transcript_path)
        result = {"times": dict(entry["times"]), "tasks": {k: dict(v) for k, v in entry["tasks"].items()},
                  "active_ids": list(entry["active_ids"])}
        messages = dict(entry["messages"])
        requests = dict(entry["requests"])
        agents = dict(entry["agents"])
    if state_dir:
        try:
            with open(_events_path(transcript_path, state_dir), encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                        task = result["tasks"].get(event["id"])
                        if task is None:
                            continue
                        _merge_completion(task, event)
                    except (ValueError, TypeError, KeyError):
                        continue
        except FileNotFoundError:
            pass
        if message_costs.config().get("enabled", True):
            message_costs.attach(transcript_path, state_dir, result["tasks"], requests, agents,
                                 messages, result["times"], result["active_ids"] if running else ())
            if running:
                message_costs.refresh_progress(transcript_path, state_dir, result["tasks"], result["active_ids"])
    if include_messages:
        result["messages"] = [
            {"id": key, "sent_at": result["times"][key], **value, **result["tasks"].get(key, {})}
            for key, value in messages.items()
        ]
        result["messages"].sort(key=lambda item: datetime.fromisoformat(item["sent_at"].replace("Z", "+00:00")), reverse=True)
    return result


def record_stop(transcript_path: str, state_dir: str, *, message_ids: list | None = None,
                completed_at: str | None = None, source: str = "stop_hook") -> dict:
    data = snapshot(transcript_path, state_dir)
    completed_at = completed_at or datetime.now(timezone.utc).isoformat()
    ended = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    if ended.tzinfo is None or ended > datetime.now(timezone.utc):
        raise ValueError("invalid completion time")
    events = []
    for message_id in data["active_ids"] if message_ids is None else message_ids:
        if message_id not in data["tasks"]:
            raise ValueError("unknown user message")
        started = datetime.fromisoformat(data["times"][message_id].replace("Z", "+00:00"))
        if ended < started:
            raise ValueError("completion precedes message")
        task = data["tasks"][message_id]
        key = "server_completed_at" if source == "stop_hook" else "ui_completed_at"
        if task.get(key) and ended <= datetime.fromisoformat(task[key].replace("Z", "+00:00")):
            continue
        events.append({"id": message_id, "completed_at": completed_at, "source": source})
    if events:
        os.makedirs(state_dir, exist_ok=True)
        payload = ("".join(json.dumps(e) + "\n" for e in events)).encode("utf-8")
        # Single append, also safe when the Stop hook and UI report together.
        fd = os.open(_events_path(transcript_path, state_dir), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        import hook_log
        for event in events:
            server_end = data["tasks"][event["id"]].get("server_completed_at")
            delay = (ended - datetime.fromisoformat(server_end.replace("Z", "+00:00"))).total_seconds() if server_end else None
            hook_log.log("message-times", f"session={os.path.basename(transcript_path)} message={event['id']} "
                         f"source={source} completed_at={completed_at} ui_delay_seconds={delay if source == 'ui_stop' else None}")
        try:
            message_costs.record_end(transcript_path, state_dir, [e["id"] for e in events],
                                     force=source == "stop_hook")
        except (OSError, ValueError, TypeError):
            # A quota endpoint failure must not lose the task completion time.
            import hook_log
            hook_log.log("message-costs", "completion observation failed")
    return snapshot(transcript_path, state_dir)
