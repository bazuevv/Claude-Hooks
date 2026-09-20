import json
import tempfile
import unittest
from pathlib import Path

from repair_compaction_history import measured_compactions, repairs


class RepairTests(unittest.TestCase):
    def test_zero_usage_and_multiple_compactions_repair_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread = "00000000-0000-0000-0000-000000000001"
            rollout = root / ("rollout-" + thread + ".jsonl")
            events = []
            audit = []
            for index, before, after in [(1, 237218, 235375), (2, 243603, 236117)]:
                ts = f"2026-09-17T09:2{index}:00Z"
                for count in [before, 0]:
                    events.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": count}}}})
                events.append({"type": "compacted", "timestamp": ts, "payload": {}})
                for count in [0, after]:
                    events.append({"type": "event_msg", "payload": {"type": "token_count", "info": {
                        "last_token_usage": {"input_tokens": count}}}})
                audit.append({"event_id": thread + f":item:c{index}", "thread_id": thread,
                              "turn_id": "same-turn", "phase": "completed", "completed_at": ts,
                              "session_hash": "session", "model": "test", "context_window": 258400})
            rollout.write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
            (root / "codex-compactions.jsonl").write_text("\n".join(map(json.dumps, audit)), encoding="utf-8")
            old = {"event_id": "turn:same-turn", "session_hash": "session", "ts": audit[0]["completed_at"],
                   "context_before": 234591, "context_after": 235375}
            ui = root / "codex-context-events.jsonl"
            ui.write_text(json.dumps(old) + "\n", encoding="utf-8")
            self.assertEqual(len(list(measured_compactions(rollout))), 2)
            updates = repairs(root, root)
            self.assertEqual([(r["context_before"], r["context_after"]) for r in updates],
                             [(237218, 235375), (243603, 236117)])
            self.assertEqual(updates[0]["event_id"], old["event_id"])
            self.assertEqual(updates[1]["event_id"], audit[1]["event_id"])
            with ui.open("a", encoding="utf-8") as handle:
                for event in updates:
                    handle.write(json.dumps(event) + "\n")
            self.assertEqual(repairs(root, root), [])


if __name__ == "__main__":
    unittest.main()
