import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import message_timestamps
import usage_history_store


class MessageTimestampsTests(unittest.TestCase):
    def setUp(self):
        message_timestamps._CACHE.clear()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root_patch = patch.object(usage_history_store, "ROOT", Path(temporary.name) / "history")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        log_patch = patch("hook_log.log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_chat_history_is_opt_in_and_excludes_tool_and_meta_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            records = [
                {"type": "user", "uuid": "one", "timestamp": "2020-01-01T10:00:00Z",
                 "message": {"content": "<b>actual user text</b>"}},
                {"type": "assistant", "timestamp": "2020-01-01T10:00:15Z", "message": {"stop_reason": "end_turn"}},
                {"type": "user", "uuid": "meta", "isMeta": True, "timestamp": "2020-01-01T10:00:20Z",
                 "message": {"content": "internal instruction"}},
                {"type": "user", "uuid": "two", "timestamp": "2020-01-01T10:00:30Z", "message": {"content": [
                    {"type": "image", "source": {"data": "not-returned"}}, {"type": "text", "text": "with image"}]}},
                {"type": "user", "uuid": "tool", "timestamp": "2020-01-01T10:00:31Z",
                 "message": {"content": [{"type": "tool_result", "content": "internal output"}]}},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            self.assertNotIn("messages", message_timestamps.snapshot(str(path)))
            history = message_timestamps.snapshot(str(path), include_messages=True)["messages"]
            self.assertEqual([m["id"] for m in history], ["two", "one"])
            self.assertEqual(history[0]["text"], "with image")
            self.assertEqual(history[0]["attachments"], 1)
            self.assertEqual(history[1]["completed_at"], "2020-01-01T10:00:15Z")
            self.assertNotIn("not-returned", json.dumps(history))

    def test_task_spans_tools_and_restores_final_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            records = [
                {"type": "user", "uuid": "prompt", "timestamp": "2020-01-01T10:00:00Z"},
                {"type": "assistant", "timestamp": "2020-01-01T10:00:10Z", "message": {"stop_reason": "tool_use"}},
                {"type": "user", "uuid": "tool", "timestamp": "2020-01-01T10:00:30Z",
                 "message": {"content": [{"type": "tool_result"}]}},
                {"type": "user", "uuid": "meta", "isMeta": True, "timestamp": "2020-01-01T10:00:40Z"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            data = message_timestamps.snapshot(str(path))
            self.assertEqual(data["active_ids"], ["prompt"])
            self.assertEqual(data["tasks"]["prompt"], {})
            final = {"type": "assistant", "timestamp": "2020-01-01T10:01:35Z", "message": {"stop_reason": "end_turn"}}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(final) + "\n")
            self.assertEqual(message_timestamps.snapshot(str(path))["tasks"]["prompt"]["completed_at"], final["timestamp"])
            message_timestamps._CACHE.clear()
            self.assertEqual(message_timestamps.snapshot(str(path))["tasks"]["prompt"]["completed_at"], final["timestamp"])
            # A tool continuation after a provisional final response reopens the task.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**final, "message": {"stop_reason": "tool_use"}}) + "\n")
            self.assertEqual(message_timestamps.snapshot(str(path))["tasks"]["prompt"], {})

    def test_host_stop_is_durable_and_overrides_transcript_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            path.write_text(json.dumps({"type": "user", "uuid": "prompt", "timestamp": "2020-01-01T10:00:00Z"}) + "\n", encoding="utf-8")
            message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T10:01:35Z", source="ui_stop")
            message_timestamps._CACHE.clear()
            data = message_timestamps.snapshot(str(path), tmp)
            self.assertEqual(data["tasks"]["prompt"]["completed_at"], "2020-01-01T10:01:35Z")
            message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T10:01:34Z", source="stop_hook")
            data = message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T10:02:00Z", source="ui_stop")
            self.assertEqual(data["tasks"]["prompt"]["completed_at"], "2020-01-01T10:02:00Z")
            self.assertEqual(data["tasks"]["prompt"]["server_completed_at"], "2020-01-01T10:01:34Z")
            self.assertEqual(data["tasks"]["prompt"]["ui_completed_at"], "2020-01-01T10:02:00Z")
            # A delayed hook or a stale UI retry cannot move completion backwards.
            message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T10:01:40Z", source="stop_hook")
            message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T10:01:45Z", source="ui_stop")
            message_timestamps._CACHE.clear()
            data = message_timestamps.snapshot(str(path), tmp, include_messages=True)
            self.assertEqual(data["messages"][0]["completed_at"], "2020-01-01T10:02:00Z")
            self.assertEqual(data["messages"][0]["source"], "ui_stop")
            with self.assertRaises(ValueError):
                message_timestamps.record_stop(str(path), tmp, message_ids=["other-session"], completed_at="2020-01-01T10:01:35Z")
            with self.assertRaises(ValueError):
                message_timestamps.record_stop(str(path), tmp, completed_at="2020-01-01T09:00:00Z")

    def test_steering_messages_share_completion_and_interrupt_is_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            records = [
                {"type": "user", "uuid": "first", "timestamp": "2020-01-01T10:00:00Z"},
                {"type": "user", "uuid": "steer", "timestamp": "2020-01-01T10:01:00Z"},
                {"type": "user", "uuid": "interrupt", "timestamp": "2020-01-01T10:01:35Z",
                 "message": {"content": "[Request interrupted by user]"}},
                {"type": "user", "uuid": "next", "timestamp": "2020-01-01T10:02:00Z"},
            ]
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            data = message_timestamps.snapshot(str(path))
            self.assertEqual(data["tasks"]["first"]["completed_at"], "2020-01-01T10:01:35Z")
            self.assertEqual(data["tasks"]["steer"]["completed_at"], "2020-01-01T10:01:35Z")
            self.assertEqual(data["active_ids"], ["next"])

    def test_original_time_survives_reload_and_incremental_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            first = {"type": "user", "uuid": "one", "timestamp": "2026-09-12T10:09:59.757Z",
                     "message": {"content": "private message"}}
            path.write_text(json.dumps(first) + "\n", encoding="utf-8")
            expected = {"one": first["timestamp"]}
            self.assertEqual(message_timestamps.collect(str(path)), expected)
            with patch("builtins.open", side_effect=AssertionError("unchanged file must not be reread")):
                self.assertEqual(message_timestamps.collect(str(path)), expected)
            message_timestamps._CACHE.clear()  # Server/window restart.
            self.assertEqual(message_timestamps.collect(str(path)), expected)
            second = {"type": "user", "uuid": "two", "timestamp": "2026-09-17T15:00:00Z"}
            line = json.dumps(second)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line[:20])
            self.assertEqual(message_timestamps.collect(str(path)), expected)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line[20:] + "\n")
            self.assertEqual(message_timestamps.collect(str(path)), {**expected, "two": second["timestamp"]})

    def test_sessions_invalid_records_and_replaced_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chat.jsonl"
            other = Path(tmp) / "other.jsonl"
            valid = {"type": "user", "uuid": "same-id", "timestamp": "2026-09-12T10:00:00Z"}
            records = [valid, {**valid, "type": "assistant", "uuid": "assistant"},
                       {**valid, "uuid": "tool-result", "message": {
                           "content": [{"type": "tool_result", "content": "tool output"}]}},
                       {**valid, "uuid": "bad", "timestamp": "not a date"},
                       {**valid, "uuid": "naive", "timestamp": "2026-09-12T10:00:00"}, None]
            path.write_text("\n".join(map(json.dumps, records)) + "\ninvalid json\n", encoding="utf-8")
            self.assertEqual(message_timestamps.collect(str(path)), {"same-id": valid["timestamp"]})
            replacement = {**valid, "timestamp": "2026-09-17T15:00:00Z"}
            other.write_text(json.dumps(replacement) + "\n", encoding="utf-8")
            self.assertEqual(message_timestamps.collect(str(other)), {"same-id": replacement["timestamp"]})
            other.replace(path)
            self.assertEqual(message_timestamps.collect(str(path)), {"same-id": replacement["timestamp"]})


if __name__ == "__main__":
    unittest.main()
