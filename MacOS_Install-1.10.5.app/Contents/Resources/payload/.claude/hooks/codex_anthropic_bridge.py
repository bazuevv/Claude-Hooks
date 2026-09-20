#!/usr/bin/env python3
"""Loopback-only Anthropic Messages facade backed by Codex app-server.

Anthropic tools are exposed as Codex dynamic tools. OAuth remains wholly owned
by the official Codex process.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import ipaddress
import json
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import chain
from typing import Any, Iterator

from codex_app_server import CodexAppServerClient, CodexAppServerError, CodexRpcError
from codex_app_server import _safe_probe
from bridge_recovery import RECOVERY_VERSION, recover_payload

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18924
DEFAULT_REQUEST_TIMEOUT = 600.0
CONTEXT_EVENTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks-runtime", "codex-context-events.jsonl",
)
BRIDGE_USAGE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks-runtime", "codex-bridge-usage.json",
)
COMPACTION_LOG_FILE = os.path.join(os.path.dirname(BRIDGE_USAGE_FILE), "codex-compactions.jsonl")
CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "patches", "claude-custom-config.toml",
)
PAYLOAD_CAPTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hooks-runtime", "codex-payloads",
)
SOURCE_MTIME = max(
    os.path.getmtime(__file__),
    os.path.getmtime(os.path.join(os.path.dirname(__file__), "codex_app_server.py")),
    os.path.getmtime(os.path.join(os.path.dirname(__file__), "bridge_recovery.py")),
)
BRIDGE_INSTRUCTIONS = """You are providing the model response inside Claude Code.
Return only the answer to the conversation supplied by the user. Never call
built-in Codex tools and never inspect the filesystem directly. When dynamic
tools are supplied, they are Claude Code's tools: call them whenever the task
requires tool use. Follow the supplied system instructions and conversation
faithfully.

Execution boundary: Codex's local sandbox and approval policy govern its
built-in tools only. The supplied dynamic tools execute in the Claude Code
host, which enforces the user's current permission mode and approvals.
For an authorized file task, use the supplied Read, Edit, Write or Bash tool;
do not infer that those host tools are read-only from Codex's local sandbox.
Respect any denial or approval requirement returned by Claude Code. Never
replace a denied host tool call with a built-in Codex filesystem operation.
When no suitable host tool is supplied, explain that limitation."""


class BridgeError(RuntimeError):
    pass


def payload_capture_enabled() -> bool:
    """Read the switch for every request so toggling needs no restart."""
    if tomllib is None:
        return False
    try:
        with open(CONFIG_FILE, "rb") as handle:
            config = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return config.get("codexPayloadCapture") is True


def capture_claude_payload(payload: dict[str, Any]) -> str | None:
    """Atomically persist one complete Anthropic request body when enabled.

    Payloads can contain prompts, file contents and base64 images. Keep them in
    the ignored runtime directory with owner-only permissions and never copy
    HTTP authorization headers into the capture.
    """
    if not payload_capture_enabled():
        return None
    try:
        os.makedirs(PAYLOAD_CAPTURE_DIR, mode=0o700, exist_ok=True)
        os.chmod(PAYLOAD_CAPTURE_DIR, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        filename = f"claude-payload-{stamp}-{uuid.uuid4().hex[:8]}.json"
        target = os.path.join(PAYLOAD_CAPTURE_DIR, filename)
        temporary = target + ".tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        # Diagnostics must never make the proxied model request fail.
        return None
    return target


def _text_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    chunks: list[str] = []
    for block in value:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _image_input(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise BridgeError("image block has no source")
    source_type = source.get("type")
    if source_type == "base64":
        media_type = source.get("media_type")
        data = source.get("data")
        if not isinstance(media_type, str) or not media_type.startswith("image/"):
            raise BridgeError("base64 image has an invalid media_type")
        if not isinstance(data, str) or not data:
            raise BridgeError("base64 image has no data")
        url = f"data:{media_type};base64,{data}"
    elif source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BridgeError("image URL must use http or https")
    else:
        raise BridgeError(f"unsupported image source: {source_type!r}")
    detail = block.get("detail")
    result: dict[str, Any] = {"type": "image", "url": url}
    if detail in ("auto", "low", "high", "original"):
        result["detail"] = detail
    return result


def _render_content(
    value: Any,
    image_inputs: list[dict[str, Any]] | None = None,
) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    rendered: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and isinstance(block.get("text"), str):
            rendered.append(block["text"])
        elif block_type == "image":
            if image_inputs is None:
                raise BridgeError("image collection is not initialized")
            image_inputs.append(_image_input(block))
            rendered.append(f'<image attachment="{len(image_inputs)}" />')
        elif block_type in ("tool_use", "server_tool_use"):
            tag = str(block_type)
            rendered.append(
                f'<{tag} id="{block.get("id", "")}" '
                f'name="{block.get("name", "")}">\n'
                f'{json.dumps(block.get("input", {}), ensure_ascii=False)}\n'
                f"</{tag}>"
            )
        elif block_type == "tool_result":
            content = _render_content(block.get("content"), image_inputs)
            rendered.append(
                f'<tool_result id="{block.get("tool_use_id", "")}" '
                f'is_error="{str(bool(block.get("is_error"))).lower()}">\n'
                f"{content}\n</tool_result>"
            )
        elif block_type in ("thinking", "redacted_thinking"):
            # Previous providers may persist private reasoning in the
            # transcript. It is neither needed nor appropriate as input to
            # another model; retain only the fact that a block existed.
            rendered.append(f'<content_block type="{block_type}" omitted="true" />')
        else:
            # Claude Code adds new server-side result blocks over time. A
            # historical block must not make the whole conversation unusable:
            # preserve its public JSON as quoted context. Known binary image
            # blocks still take the native path above.
            public = {key: item for key, item in block.items()
                      if key not in ("signature", "data")}
            rendered.append(
                f"<content_block type={json.dumps(str(block_type))}>\n"
                f"{json.dumps(public, ensure_ascii=False)}\n"
                "</content_block>"
            )
    return "\n".join(rendered)


def build_request(
    payload: dict[str, Any],
) -> tuple[str, str, list[dict[str, Any]]]:
    """Convert an Anthropic conversation to Codex instructions and inputs."""
    image_inputs: list[dict[str, Any]] = []
    system = _text_blocks(payload.get("system"))
    developer = BRIDGE_INSTRUCTIONS
    if system:
        developer += "\n\nSYSTEM INSTRUCTIONS FROM CLAUDE CODE:\n" + system

    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BridgeError("messages must be a non-empty array")
    rendered: list[str] = []
    privileged: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise BridgeError("each message must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant"):
            raise BridgeError(f"unsupported message role: {role!r}")
        content = message.get("content")
        text = _render_content(content, image_inputs)
        # Claude Code 2.1.220 may put additional privileged context in
        # `messages` instead of the top-level Anthropic `system` field.
        # Keep its precedence: it belongs in developerInstructions, not
        # among quoted user/assistant conversation turns.
        if role in ("system", "developer"):
            privileged.append((role, text))
            continue
        rendered.append(f"<{role}>\n{text}\n</{role}>")
    for role, text in privileged:
        developer += f"\n\n{role.upper()} MESSAGE FROM CLAUDE CODE:\n{text}"
    if image_inputs:
        developer += (
            "\n\nIMAGE ATTACHMENTS: Markers in the conversation refer to the "
            "attached image inputs in ascending order."
        )
    rendered.append("<assistant>\n")
    return developer, "\n\n".join(rendered), image_inputs


def build_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    """Compatibility helper for tests and text-only callers."""
    developer, prompt, _images = build_request(payload)
    return developer, prompt


def select_model(payload: dict[str, Any]) -> str | None:
    requested = payload.get("model")
    if isinstance(requested, str) and requested.startswith("gpt-"):
        return requested
    configured = os.environ.get("CODEX_BRIDGE_MODEL", "").strip()
    return configured or None


def select_effort(payload: dict[str, Any]) -> str | None:
    """Translate Claude's Messages API effort into a Codex turn override."""
    output_config = payload.get("output_config")
    candidates = [
        output_config.get("effort") if isinstance(output_config, dict) else None,
        payload.get("effort"),
    ]
    for value in candidates:
        if isinstance(value, str) and value in (
            "low", "medium", "high", "xhigh", "max",
        ):
            return value
    return None


def is_title_request(payload: dict[str, Any]) -> bool:
    """Recognize Claude Code's auxiliary structured session-title request.

    Claude Code sends this request with the same ``session_id`` as the real
    conversation.  It must not become that session's persistent Codex thread:
    otherwise the next user turn inherits the title generator's instructions
    and returns ``{"title": ...}`` into the chat.
    """
    output_config = payload.get("output_config")
    output_format = (
        output_config.get("format") if isinstance(output_config, dict) else None
    )
    if not isinstance(output_format, dict):
        return False
    schema = output_format.get("schema")
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    required = schema.get("required")
    return (
        isinstance(properties, dict)
        and set(properties) == {"title"}
        and isinstance(properties.get("title"), dict)
        and properties["title"].get("type") == "string"
        and isinstance(required, list)
        and set(required) == {"title"}
    )


def is_compaction_request(payload: dict[str, Any]) -> bool:
    """Recognize the client's text-only summary task, not quoted history."""
    messages = payload.get("messages") or []
    if not messages or messages[-1].get("role") != "user":
        return False
    text = _text_blocks(messages[-1].get("content"))
    return (
        text.startswith("CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.")
        and "Your task is to create a detailed summary of the conversation so far" in text
    )


def dynamic_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tools = payload.get("tools")
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise BridgeError("tools must be an array")
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise BridgeError("each tool must have a name")
        schema = tool.get("input_schema", {"type": "object"})
        if not isinstance(schema, dict):
            raise BridgeError(f"tool {tool['name']!r} has an invalid input_schema")
        converted.append({
            "type": "function",
            "name": tool["name"],
            "description": str(tool.get("description", "")),
            "inputSchema": schema,
        })
    return converted


def prepare_dynamic_tools(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Rename names reserved by Codex and retain the Anthropic names."""
    prepared = dynamic_tools(payload)
    original_names: dict[str, str] = {}
    used = {tool["name"] for tool in prepared}
    for index, tool in enumerate(prepared):
        original = tool["name"]
        safe = original
        if original.startswith("mcp__"):
            safe = f"claude_mcp_tool_{index}"
            while safe in used:
                safe += "_"
            used.add(safe)
            tool["name"] = safe
            tool["description"] = (
                f"Claude Code tool {original}. " + tool["description"]
            ).strip()
        original_names[safe] = original
    return prepared, original_names


def claude_session_key(payload: dict[str, Any]) -> str | None:
    """Extract the stable Claude Code session id from Anthropic metadata."""
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    direct = metadata.get("session_id")
    if isinstance(direct, str) and direct:
        return direct
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    try:
        decoded = json.loads(user_id)
    except json.JSONDecodeError:
        return None
    session_id = decoded.get("session_id") if isinstance(decoded, dict) else None
    return session_id if isinstance(session_id, str) and session_id else None


def _message_fingerprints(payload: dict[str, Any]) -> list[str]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise BridgeError("messages must be a non-empty array")
    return [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in messages]


def _compacted_history_changed(payload: dict[str, Any], previous: list[str]) -> bool:
    """A new leading Claude summary replaces the previous conversation."""
    def summary(messages):
        if not messages or messages[0].get("role") != "user":
            return None
        text = _text_blocks(messages[0].get("content")).strip()
        prefix = "This session is being continued from a previous conversation that ran out of context."
        return text if text.startswith(prefix) else None
    current = summary(payload.get("messages") or [])
    return current is not None and current != summary([json.loads(m) for m in previous])


def _followup_payload(
    payload: dict[str, Any], previous: list[str],
) -> dict[str, Any]:
    """Return the newest real user input without replaying old history.

    Claude Code may append ephemeral hook, attachment, or assistant scaffolding
    after the user's message.  Those blocks are not stable between API calls,
    so an anchor against the former last fingerprint can land after the new
    user message and make the suffix appear empty.  A persistent Codex thread
    already owns every earlier turn: the only conversation block it needs is
    the newest non-tool-result user message.
    """
    del previous  # retained in the signature for callers and focused tests
    messages = payload.get("messages") or []
    for item in reversed(messages):
        if (isinstance(item, dict) and item.get("role") == "user"
                and not _is_tool_result_only(item.get("content"))):
            result = dict(payload)
            result["messages"] = [item]
            return result
    raise BridgeError("Claude session has no new user input")


def _is_tool_result_only(content: Any) -> bool:
    return (
        isinstance(content, list) and bool(content)
        and all(isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content)
    )


def _tool_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            if isinstance(call_id, str) and call_id:
                results[call_id] = block
    return results


def _has_new_user_input(payload: dict[str, Any], previous: list[str]) -> bool:
    """Claude can append a new prompt/image to an old tool-result message."""
    def blocks(messages: list[dict[str, Any]]) -> list[str]:
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = user.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        result = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text = block.get("text", "").strip()
                if not text or text.startswith((
                    "[Request interrupted by user", "[Image: original ",
                    "<system-reminder>",
                )):
                    continue
                result.append(json.dumps({"type": kind, "text": text}, sort_keys=True))
            elif kind == "image":
                result.append(json.dumps({k: v for k, v in block.items()
                                          if k != "cache_control"}, sort_keys=True))
        return result

    current = blocks(payload.get("messages") or [])
    old = blocks([json.loads(message) for message in previous])
    return any(current.count(block) > old.count(block) for block in current)


def _dynamic_result(block: dict[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    items: list[dict[str, Any]] = []
    if isinstance(content, str):
        items.append({"type": "inputText", "text": content})
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                items.append({"type": "inputText", "text": item["text"]})
            elif item.get("type") == "image":
                image = _image_input(item)
                items.append({"type": "inputImage", "imageUrl": image["url"]})
    if not items:
        items.append({"type": "inputText", "text": ""})
    return {"contentItems": items, "success": not bool(block.get("is_error"))}


def _host_context_update(payload: dict[str, Any], previous: list[str]) -> str:
    """Forward fresh host instructions accompanying the latest user/tool result.

    Do not scan historical system messages: a resumed checkpoint retains only
    boundary fingerprints, and replaying an old plan-mode reminder is harmful.
    Tool output and user text are never promoted to application instructions.
    """
    messages = payload.get("messages") or []
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if messages[i].get("role") == "user"), len(messages))
    updates = []
    for message in messages[start:]:
        if message.get("role") not in ("system", "developer"):
            continue
        fingerprint = json.dumps(message, ensure_ascii=False, sort_keys=True)
        if fingerprint not in previous:
            text = _text_blocks(message.get("content"))
            if text:
                updates.append(text)
    return "\n\n".join(updates)


class EventRouter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, queue.Queue[dict[str, Any]]] = {}

    def register(self, thread_id: str) -> queue.Queue[dict[str, Any]]:
        target: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._queues[thread_id] = target
        return target

    def unregister(self, thread_id: str) -> None:
        with self._lock:
            self._queues.pop(thread_id, None)

    def dispatch(self, message: dict[str, Any]) -> None:
        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, dict) else None
        if not isinstance(thread_id, str):
            return
        with self._lock:
            target = self._queues.get(thread_id)
        if target is not None:
            target.put(message)


@dataclass
class TextTurn:
    message_id: str
    model: str
    chunks: Iterator[Any]
    usage: dict[str, int] = field(default_factory=dict)


@dataclass
class DynamicToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    _response: queue.Queue[dict[str, Any]]

    def resolve(self, result: dict[str, Any]) -> None:
        self._response.put(result)


@dataclass
class BridgeSession:
    key: str
    thread_id: str
    model: str
    effort: str | None
    events: queue.Queue[dict[str, Any]]
    tool_calls: queue.Queue[DynamicToolCall]
    tool_names: dict[str, str]
    tool_signature: str
    seen_messages: list[str]
    compaction_audit: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_observed_usage: dict[str, int] = field(default_factory=dict)
    resumed_from_disk: bool = False
    active_turn_id: str | None = None
    pending_tools: dict[str, DynamicToolCall] = field(default_factory=dict)
    response_lock: threading.Lock = field(default_factory=threading.Lock)
    last_usage: dict[str, int] = field(default_factory=dict)
    total_usage: dict[str, int] = field(default_factory=dict)
    context_window: int | None = None
    turns_started: int = 0
    tool_continuations: int = 0
    initial_input_chars: int | None = None
    last_input_chars: int = 0
    seen_compactions: set[str] = field(default_factory=set)
    pending_compaction: dict[str, Any] | None = None
    prior_context: int = 0
    publish_usage: bool = True


TOKEN_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
    "totalTokens": "total_tokens",
}
USAGE_READY = object()
STREAM_PING = object()
STREAM_PING_INTERVAL = 10.0
DISPATCH_TOOL = "claude_bridge_dispatch"
DISPATCH_SPEC = {
    "type": "function",
    "name": DISPATCH_TOOL,
    "description": "Call a currently available Claude host tool using its name and arguments. Host permissions apply.",
    "inputSchema": {"type": "object", "properties": {
        "name": {"type": "string"}, "arguments": {"type": "object"},
    }, "required": ["name", "arguments"]},
}


def _normalized_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for source, target in TOKEN_USAGE_FIELDS.items():
        amount = value.get(source, 0)
        if isinstance(amount, int) and amount >= 0:
            result[target] = amount
    return result


def _anthropic_usage(usage: dict[str, int]) -> dict[str, int]:
    # Codex input includes cache hits; Anthropic reports disjoint buckets.
    total = max(0, usage.get("input_tokens", 0))
    cached = min(total, max(0, usage.get("cached_input_tokens", 0)))
    created = min(total - cached, max(0, usage.get("cache_write_input_tokens", 0)))
    return {
        "input_tokens": total - cached - created,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": created,
        "output_tokens": usage.get("output_tokens", 0),
    }


class CodexTextBackend:
    """Keep one Codex thread per Claude Code session."""

    def __init__(
        self, *, timeout: float = DEFAULT_REQUEST_TIMEOUT,
        usage_state_file: str | None = None,
    ) -> None:
        self.timeout = timeout
        self.router = EventRouter()
        self._tool_lock = threading.Lock()
        self._sessions_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._sessions: dict[str, BridgeSession] = {}
        self._tool_queues: dict[str, BridgeSession] = {}
        self._usage_state_file = usage_state_file
        self._checkpoint_dir = (os.path.join(os.path.dirname(usage_state_file), "bridge-sessions")
                                if usage_state_file else None)
        self._recovery_archive_dir = os.path.join(
            os.path.dirname(usage_state_file or BRIDGE_USAGE_FILE), "recovered-history",
        )
        self._latest_usage: dict[str, Any] = self._load_usage_state()
        self.client = CodexAppServerClient(
            notification_handler=self._handle_notification,
            server_request_handler=self._handle_server_request,
        )

    def start(self) -> "CodexTextBackend":
        self.client.start()
        return self

    def close(self) -> None:
        self.client.close()

    def _checkpoint_path(self, key: str) -> str | None:
        if self._checkpoint_dir:
            return os.path.join(self._checkpoint_dir, hashlib.sha256(key.encode()).hexdigest() + ".json")
        return None

    def _checkpoint(self, session: BridgeSession) -> None:
        path = self._checkpoint_path(session.key)
        if not path or self._sessions.get(session.key) is not session:
            return
        # Codex owns the durable, compacted history. Keep only host anchors here.
        messages = session.seen_messages
        value = {"thread_id": session.thread_id,
                 "recovery_version": RECOVERY_VERSION,
                 "last_usage": session.last_observed_usage or session.last_usage,
                 "context_window": session.context_window,
                 "seen_messages": messages[:1] + messages[max(1, len(messages)-4):],
                 "tool_signature": session.tool_signature, "tool_names": session.tool_names}
        os.makedirs(self._checkpoint_dir, exist_ok=True)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)

    def _resume_checkpoint(self, key, payload, tools, tool_names, tool_signature):
        path = self._checkpoint_path(key)
        if not path or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            saved = json.load(handle)
        if saved.get("recovery_version", 0) < RECOVERY_VERSION:
            developer, _, _ = build_request(payload)
            if len(developer) > 120000:
                # Legacy recovery pinned the entire host instruction history.
                # Rebuild from this fresh host payload at the request boundary.
                # Keep the old checkpoint until the replacement is persisted.
                return None
        if _compacted_history_changed(payload, saved["seen_messages"]):
            os.unlink(path)
            return None
        # Do not silently fall back to replay on transport/auth/resume errors.
        result = self.client.request("thread/resume", {
            "threadId": saved["thread_id"], "approvalPolicy": "never", "sandbox": "read-only",
        })
        thread = result["thread"]
        session = BridgeSession(
            key=key, thread_id=thread["id"], model=select_model(payload) or "codex",
            effort=select_effort(payload), events=self.router.register(thread["id"]),
            tool_calls=queue.Queue(), tool_names=saved["tool_names"],
            tool_signature=saved["tool_signature"], seen_messages=saved["seen_messages"],
            resumed_from_disk=True,
            last_usage=saved.get("last_usage", {}), context_window=saved.get("context_window"),
            last_observed_usage=dict(saved.get("last_usage", {})),
        )
        with self._tool_lock:
            self._tool_queues[session.thread_id] = session
        with self._sessions_lock:
            self._sessions[key] = session
        return session

    def snapshot(self) -> dict[str, Any]:
        """Safe account/model/limit data; never exposes OAuth credentials."""
        result = _safe_probe(self.client.snapshot())
        with self._usage_lock:
            result["bridgeUsage"] = dict(self._latest_usage)
        return result

    def _load_usage_state(self) -> dict[str, Any]:
        if not self._usage_state_file:
            return {}
        try:
            with open(self._usage_state_file, encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save_usage_state(self, value: dict[str, Any]) -> None:
        if not self._usage_state_file or not value.get("last"):
            return
        try:
            os.makedirs(os.path.dirname(self._usage_state_file), exist_ok=True)
            temporary = self._usage_state_file + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
            os.replace(temporary, self._usage_state_file)
        except OSError:
            return

    def _handle_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method in ("item/started", "item/completed", "thread/compacted"):
            params = message.get("params") or {}
            item = params.get("item") or {}
            if method == "thread/compacted" or item.get("type") == "contextCompaction":
                with self._tool_lock:
                    session = self._tool_queues.get(params.get("threadId"))
                if session is not None:
                    self._audit_compaction(session, params, method)
        if method == "thread/tokenUsage/updated":
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            token_usage = params.get("tokenUsage") if isinstance(params, dict) else None
            with self._tool_lock:
                session = self._tool_queues.get(thread_id)
            if session is not None and isinstance(token_usage, dict):
                last = _normalized_usage(token_usage.get("last"))
                # Compaction can emit a synthetic all-zero `last` usage before
                # the first real model request. It is not a context measurement.
                measured = last.get("input_tokens", 0) > 0
                if measured:
                    session.last_observed_usage = dict(last)
                total = _normalized_usage(token_usage.get("total"))
                context_window = token_usage.get("modelContextWindow")
                if measured:
                    session.last_usage.clear()
                    session.last_usage.update(last)
                session.total_usage = total
                session.context_window = (
                    context_window if isinstance(context_window, int) else None
                )
                if measured and session.pending_compaction is not None:
                    session.pending_compaction["context_after"] = last.get(
                        "input_tokens", 0,
                    )
                    self._append_context_event(session.pending_compaction)
                    session.pending_compaction = None
                self._publish_session(session)
        elif method in ("item/completed", "thread/compacted"):
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            item = params.get("item") if isinstance(params, dict) else None
            is_compaction = (
                method == "thread/compacted"
                or (isinstance(item, dict) and item.get("type") == "contextCompaction")
            )
            with self._tool_lock:
                session = self._tool_queues.get(thread_id)
            if session is not None and is_compaction:
                self._record_compaction(session, params or {})
        self.router.dispatch(message)

    def _audit_compaction(self, session, params, method):
        """Audit observed Codex starts separately from the Usage UI history."""
        item_id = (params.get("item") or {}).get("id")
        turn_id = params.get("turnId") or session.active_turn_id
        key = "item:" + item_id if item_id else "turn:" + str(turn_id)
        if not item_id:
            matches = [k for k, v in session.compaction_audit.items() if v["turn_id"] == turn_id]
            if matches:
                key = matches[-1]
        previous = session.compaction_audit.get(key)
        phase = "started" if method == "item/started" else "completed"
        if previous and (previous["phase"] == phase or previous["phase"] == "completed"):
            return previous
        now = datetime.now(timezone.utc).isoformat()
        if previous:
            event = {**previous, "phase": phase, "completed_at": now}
        else:
            usage = session.last_observed_usage or session.last_usage
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens", 0)
            event = {
                "event_id": session.thread_id + ":" + key,
                "thread_id": session.thread_id, "turn_id": turn_id,
                "session_hash": hashlib.sha256(session.key.encode()).hexdigest(),
                "model": session.model, "phase": phase,
                "started_at": now if phase == "started" else None,
                "context_window": session.context_window,
                "context_tokens_at_start": (input_tokens + output_tokens
                                            if phase == "started" and input_tokens is not None else None),
                "input_tokens_at_start": input_tokens if phase == "started" else None,
                "measurement": "latest Codex token usage before start" if phase == "started" else "start event not observed",
            }
            if phase == "completed":
                event["completed_at"] = now
        session.compaction_audit[key] = event
        self._append_context_event(event, COMPACTION_LOG_FILE)
        return event

    def _record_compaction(
        self, session: BridgeSession, params: dict[str, Any],
    ) -> None:
        """Persist one safe Usage-history row for each Codex compaction."""
        if not session.publish_usage:
            return
        audit = self._audit_compaction(session, params, "item/completed")
        event_id = audit["event_id"]
        if event_id in session.seen_compactions:
            return
        session.seen_compactions.add(event_id)
        before = audit.get("input_tokens_at_start")
        if before is None:
            before = session.last_observed_usage.get("input_tokens")
        event = {
            "kind": "compact",
            "event_id": event_id,
            "thread_id": session.thread_id,
            "turn_id": audit["turn_id"],
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_hash": hashlib.sha256(session.key.encode()).hexdigest(),
            "model": session.model,
            "context_before": before,
            "context_window": session.context_window,
        }
        session.pending_compaction = event
        # Сохраняем событие сразу. Если Extension Host или мост умрёт до
        # следующей tokenUsage notification, строка сжатия всё равно останется;
        # поздняя usage допишет вторую версию с context_after, а читатель
        # объединит обе по event_id.
        self._append_context_event(event)

    @staticmethod
    def _append_context_event(event: dict[str, Any], path: str | None = None) -> None:
        try:
            path = path or CONTEXT_EVENTS_FILE
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            return

    def _publish_session(self, session: BridgeSession) -> None:
        """Expose only counters and model metadata, never prompts or ids."""
        if not session.publish_usage:
            return
        session_hash = hashlib.sha256(session.key.encode()).hexdigest()
        value = {
            "model": session.model,
            "effort": session.effort,
            "last": dict(session.last_usage),
            "total": dict(session.total_usage),
            "model_context_window": session.context_window,
            "turns_started": session.turns_started,
            "tool_continuations": session.tool_continuations,
            "initial_input_chars": session.initial_input_chars,
            "last_input_chars": session.last_input_chars,
            "updated_at": int(time.time()),
            "session_key_hash": session_hash,
        }
        with self._usage_lock:
            previous = self._latest_usage
            # turn/start очищает session.last_usage, чтобы в Anthropic stream
            # не ушли счётчики прошлого хода. Для панели при этом сохраняем
            # последний подтверждённый снимок до прихода новой телеметрии.
            if (not value["last"] and previous.get("session_key_hash") == session_hash
                    and previous.get("last")):
                value["last"] = dict(previous["last"])
                value["total"] = dict(previous.get("total") or {})
                value["model_context_window"] = (
                    value["model_context_window"]
                    or previous.get("model_context_window")
                )
            self._latest_usage = value
            self._save_usage_state(value)

    def begin(self, payload: dict[str, Any]) -> TextTurn:
        # Title, summary and tool-free classifiers share the host session ID.
        # They must not replace the working thread or inherit its pending tools.
        auxiliary = (is_title_request(payload) or is_compaction_request(payload)
                     or not payload.get("tools"))
        if is_compaction_request(payload):
            # Summarization must not resume a pending host tool call or execute
            # any tools. Leave the live conversation available for continuation.
            payload = {**payload, "tools": []}
        stable_key = None if auxiliary else claude_session_key(payload)
        key = stable_key or "request:" + uuid.uuid4().hex
        tools, tool_names = prepare_dynamic_tools(payload)
        tool_signature = json.dumps(tools, ensure_ascii=False, sort_keys=True)
        with self._sessions_lock:
            session = self._sessions.get(key)
        if session is None and stable_key is not None:
            session = self._resume_checkpoint(key, payload, tools, tool_names, tool_signature)
        if session is not None:
            session.response_lock.acquire()
            try:
                return self._continue_or_start(
                    session, payload, tools, tool_names, tool_signature,
                )
            except Exception:
                if session.response_lock.locked():
                    session.response_lock.release()
                raise
        return self._start_session(
            key, payload, tools, tool_names, tool_signature, stable_key is not None,
            publish_usage=not auxiliary,
        )

    def _start_session(
        self,
        key: str,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_names: dict[str, str],
        tool_signature: str,
        persistent: bool,
        *,
        publish_usage: bool = True,
        extra_images: list[dict[str, Any]] | None = None,
    ) -> TextTurn:
        developer, prompt, image_inputs = build_request(payload)
        recovery_diagnostic = None
        if persistent and (len(prompt) + len(developer) > 120000 or len(image_inputs) > 8):
            recovery_diagnostic = {"developer_chars_before": len(developer),
                                   "prompt_chars_before": len(prompt)}
            recovered = recover_payload(payload, self._recovery_archive_dir)
            # Recovery must happen BEFORE thread/start: developerInstructions
            # are retained across compactions just like the ordinary history.
            developer, prompt, image_inputs = build_request(recovered)
            recovery_diagnostic.update(developer_chars_after=len(developer), prompt_chars_after=len(prompt))
            if len(developer) > 120000:
                raise BridgeError("Current host instructions exceed the recovery budget; history was archived")
        image_inputs.extend(extra_images or [])
        model = select_model(payload)
        params: dict[str, Any] = {
            "approvalPolicy": "never",
            "baseInstructions": BRIDGE_INSTRUCTIONS,
            "cwd": "/tmp",
            "developerInstructions": developer,
            "ephemeral": not persistent,
            "sandbox": "read-only",
        }
        if model:
            params["model"] = model
        if tools:
            params["dynamicTools"] = tools + [DISPATCH_SPEC]
        started = self.client.request("thread/start", params)
        thread = started.get("thread") if isinstance(started, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str):
            raise BridgeError("thread/start returned no thread id")
        actual_model = thread.get("model") or model or "codex"
        actual_effort = thread.get("reasoningEffort")
        session = BridgeSession(
            key=key,
            thread_id=thread_id,
            model=str(actual_model),
            effort=actual_effort if isinstance(actual_effort, str) else None,
            events=self.router.register(thread_id),
            tool_calls=queue.Queue(),
            tool_names=tool_names,
            tool_signature=tool_signature,
            seen_messages=_message_fingerprints(payload),
            publish_usage=publish_usage,
        )
        self._publish_session(session)
        session.response_lock.acquire()
        with self._tool_lock:
            self._tool_queues[thread_id] = session
        if persistent:
            with self._sessions_lock:
                self._sessions[key] = session
        try:
            self._start_turn(
                session, prompt, image_inputs,
                model=model, effort=select_effort(payload),
            )
            if recovery_diagnostic is not None:
                self._append_context_event({
                    **recovery_diagnostic, "ts": datetime.now(timezone.utc).isoformat(),
                    "session_hash": hashlib.sha256(key.encode()).hexdigest(),
                    "thread_id": thread_id, "recovery_version": RECOVERY_VERSION,
                }, os.path.join(os.path.dirname(self._recovery_archive_dir), "bridge-recovery-events.jsonl"))
        except Exception:
            self._discard_session(session)
            session.response_lock.release()
            raise
        return self._text_turn(session)

    def _continue_or_start(
        self,
        session: BridgeSession,
        payload: dict[str, Any],
        tools: list[dict[str, Any]],
        tool_names: dict[str, str],
        tool_signature: str,
    ) -> TextTurn:
        if _compacted_history_changed(payload, session.seen_messages):
            # Even a matching tool_result must not resume the pre-compaction
            # Codex turn: its old context would immediately trigger compact again.
            if session.active_turn_id:
                try:
                    self.client.request("turn/interrupt", {
                        "threadId": session.thread_id, "turnId": session.active_turn_id,
                    })
                except CodexRpcError as exc:
                    if not (exc.method == "turn/interrupt" and isinstance(exc.error, dict)
                            and exc.error.get("code") == -32600
                            and exc.error.get("message") == "no active turn to interrupt"):
                        raise
            for call_id, call in session.pending_tools.items():
                supplied = _tool_results(payload).get(call_id)
                call.resolve(_dynamic_result(supplied or {
                    "is_error": True,
                    "content": "Conversation compacted; tool outcome unknown. Verify state before retrying.",
                }))
            session.pending_tools.clear()
            session.active_turn_id = None
            self._discard_session(session)
            session.response_lock.release()
            return self._start_session(
                session.key, payload, tools, tool_names, tool_signature, True,
            )
        if session.active_turn_id:
            supplied = _tool_results(payload)
            # Apply host mode transitions BEFORE unblocking the model. Otherwise
            # ExitPlanMode's result resumes generation under stale plan rules.
            host_update = _host_context_update(payload, session.seen_messages)
            if host_update and any(k in session.pending_tools for k in supplied):
                try:
                    self.client.request("turn/steer", {
                        "threadId": session.thread_id,
                        "expectedTurnId": session.active_turn_id,
                        "input": [{"type": "text", "text":
                                   "Apply the accompanying Claude host instruction update "
                                   "when continuing after the pending tool result."}],
                        "additionalContext": {"claude_host_update": {
                            "kind": "application", "value": host_update,
                        }},
                    })
                except CodexRpcError as exc:
                    if not (exc.method == "turn/steer" and isinstance(exc.error, dict)
                            and exc.error.get("code") == -32600
                            and exc.error.get("message") == "no active turn to steer"):
                        raise
                    return self._restart_tool_continuation(session, payload, host_update)
            matched = 0
            for call_id, block in supplied.items():
                call = session.pending_tools.pop(call_id, None)
                if call is not None:
                    call.resolve(_dynamic_result(block))
                    matched += 1
            if not matched:
                # Reload/Stop can abandon a host tool result. A genuinely new
                # user turn supersedes that wait; retries of the old request do
                # not. System/hook scaffolding may follow the user message.
                messages = payload.get("messages") or []
                newest_user = next((m for m in reversed(messages)
                                    if m.get("role") == "user"), None)
                if newest_user is not None:
                    content = newest_user.get("content")
                    fingerprint = json.dumps(newest_user, ensure_ascii=False, sort_keys=True)
                    is_new = (_message_fingerprints(payload).count(fingerprint)
                              > session.seen_messages.count(fingerprint))
                    if is_new and _has_new_user_input(payload, session.seen_messages):
                        try:
                            self.client.request("turn/interrupt", {
                                "threadId": session.thread_id,
                                "turnId": session.active_turn_id,
                            })
                        except CodexRpcError as exc:
                            # The old turn can finish while Claude is waiting
                            # for user input. Cancellation is already satisfied.
                            error = exc.error
                            if not (exc.method == "turn/interrupt"
                                    and isinstance(error, dict)
                                    and error.get("code") == -32600
                                    and error.get("message") == "no active turn to interrupt"):
                                raise
                        for call in session.pending_tools.values():
                            call.resolve(_dynamic_result({
                                "is_error": True,
                                "content": "Client interrupted the tool wait. Execution outcome is unknown; verify state before retrying.",
                            }))
                        session.pending_tools.clear()
                        session.active_turn_id = None
                        # Preserve the compacted thread after an interruption.
                        # The next turn only needs the new user input.
                        return self._continue_or_start(
                            session, payload, tools, tool_names, tool_signature,
                        )
                raise BridgeError("Codex turn is waiting for a Claude tool_result")
            session.tool_continuations += matched
            session.seen_messages = _message_fingerprints(payload)
            self._publish_session(session)
            return self._text_turn(session)

        followup = _followup_payload(payload, session.seen_messages)
        if session.resumed_from_disk:
            # A restart may happen between a host tool call and its result.
            # Supply the newest result instead of repeating the older user task.
            newest = next((m for m in reversed(payload.get("messages") or [])
                           if m.get("role") == "user"), None)
            if newest is not None:
                followup = {**payload, "messages": [newest]}
        previous_tools = json.loads(session.tool_signature)
        previous_schemas = {t["name"]: t.get("inputSchema") for t in previous_tools}
        current_schemas = {t["name"]: t.get("inputSchema") for t in tools}
        if previous_schemas != current_schemas or session.tool_names != tool_names:
            # The stable dispatcher exposes changed tools without losing the
            # server's compacted history. Never restart for catalog changes.
            session.tool_names = tool_names
            session.tool_signature = tool_signature
        if tool_signature != session.tool_signature:
            # Claude can reorder or refresh tool descriptions between turns.
            # Never replay a long transcript merely because that metadata
            # changed; the live Codex thread already owns the conversation.
            session.tool_names = tool_names
            session.tool_signature = tool_signature
        _developer, prompt, image_inputs = build_request(followup)
        if session.resumed_from_disk:
            prompt = ("The Claude host reconnected. Continue from your saved history. "
                      "Any tool results below are observations supplied by the host; "
                      "do not rerun completed actions.\n" + prompt)
            session.resumed_from_disk = False
        if previous_schemas != current_schemas:
            prompt = ("Updated Claude tool catalog. Use claude_bridge_dispatch for these tools; "
                      "only listed tools are available, and host permissions still apply.\n"
                      + json.dumps(tools, ensure_ascii=False) + "\n\n" + prompt)
        host_update = _host_context_update(payload, session.seen_messages)
        session.seen_messages = _message_fingerprints(payload)
        self._start_turn(
            session, prompt, image_inputs,
            model=select_model(payload), effort=select_effort(payload),
            host_update=host_update,
        )
        return self._text_turn(session)

    def _restart_tool_continuation(
        self, session: BridgeSession, payload: dict[str, Any], host_update: str,
    ) -> TextTurn:
        """Recover a host result whose Codex turn ended during the tool wait."""
        pending = dict(session.pending_tools)
        supplied = _tool_results(payload)
        # Ordinary followups deliberately skip tool-only messages. Here that
        # would replay the old user task and lose the plan approval instead.
        newest = next(m for m in reversed(payload["messages"]) if m.get("role") == "user")
        messages = []
        for message in payload["messages"]:
            if message is newest:
                messages.append(message)
            elif message.get("role") == "user" and isinstance(message.get("content"), list):
                results = [block for block in message["content"]
                           if isinstance(block, dict) and block.get("type") == "tool_result"
                           and block.get("tool_use_id") in pending]
                if results:
                    messages.append({"role": "user", "content": results})
        continuation = {**payload, "messages": messages}
        _, prompt, images = build_request(continuation)
        prompt = (
            "The previous Codex turn ended while the Claude host was handling tools. "
            "Continue from the saved thread history and the host observations below. "
            "Do not repeat completed actions.\n" + prompt
        )
        # Commit the continuation only after turn/start succeeds. A transport or
        # RPC failure must leave the pending result available for a host retry.
        self._start_turn(
            session, prompt, images, model=select_model(payload),
            effort=select_effort(payload), host_update=host_update,
        )
        matched = 0
        for call_id, call in pending.items():
            block = supplied.get(call_id)
            if block is not None:
                matched += 1
            call.resolve(_dynamic_result(block if block is not None else {
                "is_error": True,
                "content": "Previous turn ended; tool outcome unknown. Verify state before retrying.",
            }))
            session.pending_tools.pop(call_id, None)
        session.tool_continuations += matched
        session.seen_messages = _message_fingerprints(payload)
        self._publish_session(session)
        return self._text_turn(session)

    def _start_turn(
        self,
        session: BridgeSession,
        prompt: str,
        image_inputs: list[dict[str, Any]],
        *,
        model: str | None = None,
        effort: str | None = None,
        host_update: str = "",
    ) -> None:
        session.prior_context = session.last_usage.get(
            "input_tokens", session.prior_context,
        )
        session.last_usage.clear()
        params: dict[str, Any] = {
            "threadId": session.thread_id,
            "input": [{"type": "text", "text": prompt}] + image_inputs,
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        if host_update:
            params["additionalContext"] = {"claude_host_update": {
                "kind": "application", "value": host_update,
            }}
        turn = self.client.request("turn/start", params)
        turn_obj = turn.get("turn") if isinstance(turn, dict) else None
        turn_id = turn_obj.get("id") if isinstance(turn_obj, dict) else None
        if not isinstance(turn_id, str):
            raise BridgeError("turn/start returned no turn id")
        session.active_turn_id = turn_id
        if model:
            session.model = model
        if effort:
            session.effort = effort
        session.turns_started += 1
        session.last_input_chars = len(prompt)
        if session.initial_input_chars is None:
            session.initial_input_chars = len(prompt)
        self._publish_session(session)

    def _text_turn(self, session: BridgeSession) -> TextTurn:
        return TextTurn(
            message_id="msg_" + uuid.uuid4().hex,
            model=session.model,
            chunks=self._locked_chunks(session),
            usage=session.last_usage,
        )

    def _locked_chunks(self, session: BridgeSession) -> Iterator[Any]:
        try:
            yield from self._chunks(session)
        finally:
            try:
                self._checkpoint(session)
            finally:
                session.response_lock.release()

    def _discard_session(self, session: BridgeSession) -> None:
        path = self._checkpoint_path(session.key)
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                saved = json.load(handle)
            if saved.get("thread_id") == session.thread_id:
                os.unlink(path)
        self.router.unregister(session.thread_id)
        with self._tool_lock:
            self._tool_queues.pop(session.thread_id, None)
        with self._sessions_lock:
            if self._sessions.get(session.key) is session:
                self._sessions.pop(session.key, None)

    def _handle_server_request(self, message: dict[str, Any]) -> dict[str, Any]:
        method = message.get("method")
        if method != "item/tool/call":
            raise BridgeError(f"unsupported Codex server request: {method}")
        params = message.get("params")
        if not isinstance(params, dict):
            raise BridgeError("dynamic tool request has no params")
        thread_id = params.get("threadId")
        with self._tool_lock:
            session = self._tool_queues.get(thread_id)
        if session is None:
            raise BridgeError("dynamic tool request belongs to an inactive thread")
        arguments = params.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise BridgeError(f"invalid dynamic tool arguments: {exc}") from exc
        if not isinstance(arguments, dict):
            raise BridgeError("dynamic tool arguments must be an object")
        name = str(params.get("tool") or "")
        if name == DISPATCH_TOOL:
            name = arguments.get("name")
            arguments = arguments.get("arguments")
            if name not in session.tool_names.values() or not isinstance(arguments, dict):
                raise BridgeError("dispatcher requested an unavailable tool or invalid arguments")
        else:
            name = session.tool_names.get(name)
            if name is None:
                raise BridgeError("tool is no longer available in the Claude host")
        response: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        call = DynamicToolCall(
            call_id=str(params.get("callId") or "toolu_" + uuid.uuid4().hex),
            name=name,
            arguments=arguments,
            _response=response,
        )
        session.pending_tools[call.call_id] = call
        session.tool_calls.put(call)
        try:
            return response.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise BridgeError("Claude Code did not accept the dynamic tool call") from exc

    def _chunks(
        self,
        session: BridgeSession,
    ) -> Iterator[Any]:
        deadline = time.monotonic() + self.timeout
        next_ping = time.monotonic() + STREAM_PING_INTERVAL
        delegated = False
        try:
            while True:
                if time.monotonic() >= next_ping:
                    yield STREAM_PING
                    next_ping = time.monotonic() + STREAM_PING_INTERVAL
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if session.active_turn_id:
                        self.client.request(
                            "turn/interrupt", {"threadId": session.thread_id},
                        )
                    raise BridgeError("Codex turn timed out")
                try:
                    tool_call = session.tool_calls.get_nowait()
                except queue.Empty:
                    tool_call = None
                if tool_call is not None:
                    if session.pending_tools.get(tool_call.call_id) is not tool_call:
                        continue  # A recovered turn already retired this call.
                    delegated = True
                    yield tool_call
                    return
                try:
                    event = session.events.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                method = event.get("method")
                params = event.get("params") or {}
                event_turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
                if (session.active_turn_id
                        and event_turn_id not in (None, session.active_turn_id)):
                    continue
                if method == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if isinstance(delta, str) and delta:
                        yield delta
                elif method == "thread/tokenUsage/updated" and session.last_usage:
                    yield USAGE_READY
                elif method == "turn/completed":
                    turn = params.get("turn") or {}
                    status = turn.get("status")
                    if status != "completed":
                        error = turn.get("error") or {}
                        raise BridgeError(error.get("message") or f"turn {status}")
                    session.active_turn_id = None
                    # After automatic compaction App Server can emit the
                    # completion before its final tokenUsage notification.
                    # Give that telemetry a short grace period so Claude's
                    # message_start and transcript do not permanently record
                    # zero input/cache tokens for an otherwise valid turn.
                    usage_deadline = min(deadline, time.monotonic() + 0.5)
                    while not session.last_usage and time.monotonic() < usage_deadline:
                        try:
                            trailing = session.events.get(
                                timeout=max(
                                    0.001,
                                    min(0.1, usage_deadline - time.monotonic()),
                                ),
                            )
                        except queue.Empty:
                            continue
                        if (trailing.get("method") == "thread/tokenUsage/updated"
                                and session.last_usage):
                            yield USAGE_READY
                            break
                    return
                elif method == "error":
                    error = params.get("error") or {}
                    raise BridgeError(error.get("message") or "Codex turn failed")
        finally:
            # A yielded tool call intentionally leaves the Codex turn alive.
            # Claude Code returns tool_result in its next Anthropic request.
            if session.active_turn_id and not delegated:
                try:
                    self.client.request(
                        "turn/interrupt", {"threadId": session.thread_id},
                    )
                except CodexAppServerError:
                    pass
                session.active_turn_id = None


def message_object(
    message_id: str,
    model: str,
    text: str = "",
    *,
    content: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": _anthropic_usage(usage or {}),
    }


def stream_events(turn: TextTurn) -> Iterator[tuple[str, dict[str, Any]]]:
    chunks = turn.chunks
    pending = []
    for first in chunks:
        if first is STREAM_PING:
            yield "ping", {"type": "ping"}
            continue
        if first is USAGE_READY:
            break
        pending.append(first)

    # Priming the Codex iterator lets tokenUsage/updated run before the
    # Anthropic message_start frame is serialized.  Without it Claude Code
    # permanently records input/cache usage as zero even though the final
    # bridge snapshot contains the correct values.
    yield "message_start", {
        "type": "message_start",
        "message": message_object(
            turn.message_id, turn.model, "", usage=turn.usage,
        ) | {
            "content": [], "stop_reason": None,
        },
    }
    index = 0
    text_open = False
    emitted = False
    stop_reason = "end_turn"
    for chunk in chain(pending, chunks):
        if chunk is STREAM_PING:
            yield "ping", {"type": "ping"}
            continue
        if chunk is USAGE_READY:
            continue
        if isinstance(chunk, str):
            if not text_open:
                yield "content_block_start", {
                    "type": "content_block_start", "index": index,
                    "content_block": {"type": "text", "text": ""},
                }
                text_open = True
                emitted = True
            yield "content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": chunk},
            }
        elif isinstance(chunk, DynamicToolCall):
            if text_open:
                yield "content_block_stop", {"type": "content_block_stop", "index": index}
                index += 1
                text_open = False
            emitted = True
            yield "content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {
                    "type": "tool_use", "id": chunk.call_id,
                    "name": chunk.name, "input": {},
                },
            }
            yield "content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(chunk.arguments, ensure_ascii=False),
                },
            }
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
            stop_reason = "tool_use"
            break
    if hasattr(chunks, "close"):
        chunks.close()
    if text_open:
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    elif not emitted:
        yield "content_block_start", {
            "type": "content_block_start", "index": index,
            "content_block": {"type": "text", "text": ""},
        }
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    yield "message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": turn.usage.get("output_tokens", 0)},
    }
    yield "message_stop", {"type": "message_stop"}


def collect_message(turn: TextTurn) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    text: list[str] = []
    stop_reason = "end_turn"
    chunks = turn.chunks
    for chunk in chunks:
        if isinstance(chunk, str):
            text.append(chunk)
        elif isinstance(chunk, DynamicToolCall):
            if text:
                content.append({"type": "text", "text": "".join(text)})
                text.clear()
            content.append({
                "type": "tool_use", "id": chunk.call_id,
                "name": chunk.name, "input": chunk.arguments,
            })
            stop_reason = "tool_use"
            break
    if hasattr(chunks, "close"):
        chunks.close()
    if text or not content:
        content.append({"type": "text", "text": "".join(text)})
    return message_object(
        turn.message_id, turn.model, content=content, stop_reason=stop_reason,
        usage=turn.usage,
    )


class BridgeHttpServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], backend: CodexTextBackend):
        self.backend = backend
        super().__init__(address, BridgeHandler)


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    server: BridgeHttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Request bodies and authorization headers must never reach a log.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            # A rejected POST may still have an unread body. Do not parse it
            # as the next request on this HTTP/1.1 connection.
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {
                "ok": True,
                "service": "claude-openai-bridge",
                "pid": os.getpid(),
                "sourceMtime": SOURCE_MTIME,
            })
        elif self.path == "/account":
            try:
                self._json(200, {"ok": True} | self.server.backend.snapshot())
            except CodexAppServerError as exc:
                self._json(503, {"ok": False, "error": str(exc)})
        else:
            self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/v1/messages":
            self._json(404, {"error": {"type": "not_found_error", "message": "not found"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 32 * 1024 * 1024:
                raise BridgeError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise BridgeError("request body must be an object")
            capture_claude_payload(payload)
            turn = self.server.backend.begin(payload)
            if payload.get("stream") is True:
                self._stream(turn)
            else:
                self._json(200, collect_message(turn))
        except (BridgeError, CodexAppServerError, ValueError) as exc:
            self._json(400, {"type": "error", "error": {
                "type": "api_error", "message": str(exc),
            }})

    def _stream(self, turn: TextTurn) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        events = stream_events(turn)
        try:
            for event, data in events:
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BridgeError, CodexAppServerError, ValueError) as exc:
            # Headers are already sent: a second HTTP response corrupts SSE.
            payload = json.dumps({"type": "error", "error": {
                "type": "api_error", "message": str(exc),
            }})
            self.wfile.write(f"event: error\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        finally:
            events.close()
            # Closing stream_events while it primes usage does not implicitly
            # close the underlying iterator. Release its lock/turn on disconnect.
            if hasattr(turn.chunks, "close"):
                turn.chunks.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code to Codex bridge")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError as exc:
        raise SystemExit(f"--host must be a numeric loopback address: {exc}")
    if not address.is_loopback:
        raise SystemExit("refusing to expose the bridge beyond loopback")

    backend = CodexTextBackend(usage_state_file=BRIDGE_USAGE_FILE).start()
    try:
        server = BridgeHttpServer((args.host, args.port), backend)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
