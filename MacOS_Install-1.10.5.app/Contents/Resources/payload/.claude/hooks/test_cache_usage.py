#!/usr/bin/env python3
"""Tests for live OpenAI Usage metadata and compaction history."""

import hashlib
import json
import os
import tempfile
import unittest

import cache_usage


class OpenAIUsageTests(unittest.TestCase):
    def test_provider_ttl_reclassifies_mixed_history_without_changing_raw_turns(self):
        turns = [
            {"ts": "2026-09-17T10:00:00Z", "model": "gpt-6-astra"},
            {"ts": "2026-09-17T10:40:00Z", "model": "gpt-6-astra",
             "gap": 40, "miss": True, "chance": True, "verdict": "промах"},
            {"ts": "2026-09-17T11:00:00Z", "model": "claude-opus-5"},
            {"ts": "2026-09-17T11:40:00Z", "model": "claude-opus-5",
             "gap": 40, "miss": True, "chance": True, "verdict": "промах"},
        ]
        original = json.dumps(turns)
        thresholds = {"openai": 30, "anthropic": 60}
        marked = cache_usage.explain_turns(turns, [], 60, thresholds)
        self.assertFalse(marked[1]["early"])
        self.assertTrue(marked[3]["early"])
        history = cache_usage.build_history({}, marked, [])
        self.assertEqual([m["ttl_minutes"] for m in history if m["kind"] == "miss"], [30, 60])
        from unittest.mock import patch
        with patch.object(cache_usage, "account_events", return_value=[]):
            series = cache_usage.mood_series({"turns": turns}, 60, thresholds)
        self.assertEqual(series[-1]["early_misses"], 1)
        self.assertEqual(series[-1]["early_chances"], 2)
        updated = cache_usage.explain_turns(turns, [], 60, {**thresholds, "openai": 45})
        self.assertTrue(updated[1]["early"])
        self.assertEqual(json.dumps(turns), original)

    def test_provider_ttl_selection_fallback_and_boundary(self):
        thresholds = {"openai": 30, "anthropic": 60, "zai": 10, "custom": 15}
        for model, expected in [("gpt-6-astra", 30), ("claude-opus-5", 60),
                                ("glm-5.3", 10), ("unknown", 15)]:
            self.assertEqual(cache_usage.cache_ttl(60, thresholds, model=model), expected)
        self.assertEqual(cache_usage.cache_ttl(60, thresholds, provider="custom", model="glm-5.3"), 10)
        self.assertEqual(cache_usage.cache_ttl(60, thresholds, provider="openai", model="alias"), 30)
        self.assertEqual(cache_usage.cache_ttl(60, thresholds, provider="custom", model="claude-opus-5"), 15)
        for invalid in (None, False, 0, -1, "30", float("nan"), float("inf")):
            self.assertEqual(cache_usage.cache_ttl(55, {"openai": invalid}, model="gpt-6-astra"), 55)
        self.assertEqual(cache_usage.cache_ttl(55, model="gpt-6-astra"), 55)
        turns = [{"model": "gpt-6-astra", "gap": gap, "miss": True}
                 for gap in (29.99, 30, 30.01)]
        self.assertEqual([t["early"] for t in cache_usage.explain_turns(turns, [], 60, thresholds)],
                         [True, False, False])

    def test_live_usage_overlays_only_matching_claude_session(self):
        session = "session-42"
        stats = {"ok": True, "model": "glm-5.3", "context": 299300,
                 "history": [{"kind": "compact", "context_before": 0}]}
        usage = {
            "session_key_hash": hashlib.sha256(session.encode()).hexdigest(),
            "model": "gpt-5.6-sol",
            "effort": "high",
            "model_context_window": 258400,
            "turns_started": 2,
            "last": {"input_tokens": 63893, "cached_input_tokens": 50000,
                     "cache_write_input_tokens": 0},
        }
        result = cache_usage.apply_openai_usage(stats, usage, session)
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(result["context"], 63893)
        self.assertEqual(result["context_window"], 258400)
        self.assertEqual(result["effort"], "high")
        self.assertEqual(result["last"]["read"], 50000)
        self.assertEqual(result["last"]["verdict"], "попадание")
        self.assertEqual(result["last"]["cache_status"], "попадание")
        self.assertEqual(result["history"][0]["context_after"], 63893)

        stale = {"ok": True, "model": "glm-5.3", "context": 299300}
        cache_usage.apply_openai_usage(stale, usage, "another-session")
        self.assertEqual(stale["model"], "glm-5.3")
        self.assertNotIn("context_window", stale)

    def test_context_events_are_filtered_by_session(self):
        session = "session-42"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "codex-context-events.jsonl")
            events = [
                {"kind": "compact", "session_hash": hashlib.sha256(
                    session.encode()).hexdigest(), "ts": "2026-09-05T10:00:00+00:00",
                 "model": "gpt-5.6-sol", "context_before": 200000},
                {"kind": "compact", "session_hash": "other",
                 "ts": "2026-09-05T10:01:00+00:00"},
            ]
            with open(path, "w", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event) + "\n")
            result = cache_usage.context_events(session, tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["context_before"], 200000)

    def test_context_event_updates_are_collapsed_by_event_id(self):
        session = "session-42"
        session_hash = hashlib.sha256(session.encode()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "codex-context-events.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                for after in (None, 63893):
                    handle.write(json.dumps({
                        "kind": "compact", "event_id": "turn:1",
                        "session_hash": session_hash,
                        "ts": "2026-09-05T10:00:00+00:00",
                        "context_before": 247329, "context_after": after,
                    }) + "\n")
            result = cache_usage.context_events(session, tmp)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["context_after"], 63893)

    def test_codex_context_window_uses_effective_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "models_cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"models": [{
                    "slug": "gpt-test", "context_window": 272000,
                    "effective_context_window_percent": 95,
                }]}, handle)
            result = cache_usage.codex_model_context_window("gpt-test", path)
        self.assertEqual(result, 258400)

    def test_openai_fallback_marks_missing_live_cache_usage_unknown(self):
        stats = {
            "ok": True, "context": 247329,
            "last": {"read": 0, "write": 0, "verdict": "промах"},
            "history": [{"kind": "compact", "context_window": None}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "models_cache.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"models": [{
                    "slug": "gpt-test", "context_window": 272000,
                    "effective_context_window_percent": 95,
                }]}, handle)
            cache_usage.apply_openai_fallback(stats, "gpt-test", path)
        self.assertEqual(stats["context"], 247329)
        self.assertEqual(stats["context_window"], 258400)
        self.assertEqual(stats["history"][0]["context_window"], 258400)
        self.assertIsNone(stats["last"]["read"])
        self.assertEqual(stats["last"]["verdict"], "н/д")

    def test_openai_first_live_turn_is_labeled_cold_start(self):
        session = "session-42"
        stats = {"ok": True, "last": {}, "history": []}
        usage = {
            "session_key_hash": hashlib.sha256(session.encode()).hexdigest(),
            "last": {"input_tokens": 249919, "cached_input_tokens": 0,
                     "cache_write_input_tokens": 0},
            "turns_started": 1,
        }
        cache_usage.apply_openai_usage(stats, usage, session)
        self.assertEqual(stats["last"]["cache_status"], "холодный старт")
        self.assertEqual(stats["last"]["read"], 0)
        self.assertEqual(stats["last"]["write"], 0)

    def test_openai_transcript_hit_is_included_in_history_without_cache_write(self):
        records = [
            {"timestamp": "2026-09-05T12:20:42Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 63711, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 319,
                }}},
            {"timestamp": "2026-09-05T12:23:56Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 64311, "cache_read_input_tokens": 63744,
                    "cache_creation_input_tokens": 0, "output_tokens": 202,
                }}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session-42.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            result = cache_usage.collect(transcript, state_dir=tmp)

        hits = [item for item in result["history"] if item["kind"] == "hit"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["read"], 63744)
        self.assertNotIn("details", hits[0])

    def test_openai_zero_input_record_restores_compaction_and_context(self):
        records = [
            {"timestamp": "2026-09-05T06:14:32Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 247329, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 231,
                }}},
            {"timestamp": "2026-09-05T06:16:13Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 0, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 231,
                }}},
            {"timestamp": "2026-09-05T09:46:50Z", "message": {
                "model": "gpt-5.6-sol", "usage": {
                    "input_tokens": 249919, "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0, "output_tokens": 173,
                }}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session-42.jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            result = cache_usage.collect(transcript, state_dir=tmp)
        compact = next(item for item in result["history"]
                       if item["kind"] == "compact")
        self.assertEqual(result["context"], 249919)
        self.assertEqual(compact["context_before"], 247329)
        self.assertTrue(compact["inferred"])

    def test_compaction_is_included_in_history(self):
        state = {"started": "2026-09-05T09:00:00+00:00", "miss_log": []}
        history = cache_usage.build_history(state, [], [], [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400, "context_after": None,
        }])
        self.assertEqual(history, [{
            "kind": "compact", "ts": "2026-09-05T10:00:00+00:00",
            "model": "gpt-5.6-sol", "context_before": 200000,
            "context_window": 258400, "context_after": None,
        }])

    def test_only_adjacent_cache_hits_of_same_model_are_grouped(self):
        marked = [
            {"ts": "2026-09-05T10:00:00+00:00", "model": "glm-5.3",
             "verdict": "попадание", "read": 100, "explain": None},
            {"ts": "2026-09-05T10:01:00+00:00", "model": "glm-5.3",
             "verdict": "попадание", "read": 120, "explain": None},
            {"ts": "2026-09-05T10:02:00+00:00", "model": "glm-5.3",
             "verdict": "частичное", "read": 20, "explain": None},
            {"ts": "2026-09-05T10:03:00+00:00", "model": "gpt-5.6-sol",
             "verdict": "попадание", "read": 200, "explain": "model"},
            {"ts": "2026-09-05T10:04:00+00:00", "model": "gpt-5.6-sol",
             "verdict": "попадание", "read": 250, "explain": None},
        ]
        history = cache_usage.build_history({}, marked, [])
        self.assertEqual([item["kind"] for item in history],
                         ["hit", "miss", "model", "hit"])
        self.assertEqual([item["read"] for item in history if item["kind"] == "hit"],
                         [220, 450])
        self.assertEqual([item["count"] for item in history if item["kind"] == "hit"], [2, 2])

    def test_account_and_compaction_events_interleave_with_hits(self):
        marked = [
            {"ts": "2026-09-05T10:00:00+00:00", "model": "same",
             "verdict": "попадание", "read": 100, "explain": None},
            {"ts": "2026-09-05T10:02:00+00:00", "model": "same",
             "verdict": "попадание", "read": 120, "explain": None},
        ]
        events = [{"ts": "2026-09-05T10:01:00+00:00"}]
        compactions = [{"ts": "2026-09-05T13:01:30+03:00"}]
        history = cache_usage.build_history({}, list(reversed(marked)), events, compactions)
        self.assertEqual([item["kind"] for item in history],
                         ["hit", "account", "compact", "hit"])

    def test_mood_retains_compaction_after_usage_history_fills_with_hits(self):
        from unittest.mock import patch
        session = "mood-history-session"
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, session + ".jsonl")
            with open(transcript, "w", encoding="utf-8") as handle:
                for minute in range(50):
                    handle.write(json.dumps({"timestamp": f"2026-09-05T12:{minute:02d}:00Z",
                        "message": {"model": f"gpt-test-{minute % 2}", "usage": {
                            "input_tokens": 100 + minute, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 0, "output_tokens": 10}}}) + "\n")
            event = {"kind": "compact", "ts": "2026-09-05T12:01:30Z",
                     "session_hash": hashlib.sha256(session.encode()).hexdigest(),
                     "context_before": 230000, "context_after": 220000,
                     "context_window": 258400}
            with open(os.path.join(tmp, "codex-context-events.jsonl"), "w", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
            with patch.object(cache_usage, "account_events", return_value=[]):
                stats = cache_usage.collect(transcript, state_dir=tmp)
                series = cache_usage.collect_series(transcript, state_dir=tmp)
        self.assertEqual(len(stats["history"]), cache_usage.MAX_HISTORY_ITEMS)
        self.assertTrue(all(item["kind"] != "compact" for item in stats["history"]))
        self.assertEqual(stats["history"][-1]["ts"], "2026-09-05T12:49:00Z")
        self.assertEqual(stats["compactions"][0]["context_after"], 220000)
        self.assertEqual(series["compactions"], stats["compactions"])

    def test_grouping_stops_at_other_events_and_retains_latest_measurements(self):
        items = [
            {"kind": "hit", "ts": "1", "read": 100, "model": "gpt-a"},
            {"kind": "hit", "ts": "2", "read": 200, "model": "gpt-a"},
            {"kind": "compact", "ts": "3", "context_before": 220000, "context_after": 70000},
            {"kind": "compact", "ts": "4", "context_before": 230000, "context_after": 80000},
            {"kind": "hit", "ts": "5", "read": 400, "model": "gpt-a"},
            {"kind": "miss", "ts": "6", "written": 1000},
            {"kind": "miss", "ts": "7", "written": 2000},
            {"kind": "hit", "ts": "8", "read": 500, "model": "gpt-a"},
            {"kind": "hit", "ts": "9", "read": 600, "model": "gpt-b"},
        ]
        original = json.dumps(items)
        grouped = cache_usage.group_history(items)
        self.assertEqual([x["kind"] for x in grouped],
                         ["hit", "compact", "hit", "miss", "hit", "hit"])
        self.assertEqual((grouped[0]["started_ts"], grouped[0]["ts"], grouped[0]["count"], grouped[0]["read"]),
                         ("1", "2", 2, 300))
        self.assertEqual((grouped[1]["context_before"], grouped[1]["context_after"]), (230000, 80000))
        self.assertEqual(grouped[3]["written"], 3000)
        self.assertEqual(grouped[0]["details"], items[:2])
        self.assertEqual(grouped[1]["details"], items[2:4])
        self.assertEqual(grouped[3]["details"], items[5:7])
        self.assertEqual(json.dumps(items), original)

    def test_long_group_keeps_every_original_event_for_expansion(self):
        items = [{"kind": "hit", "ts": str(i), "read": i + 1} for i in range(49)]
        group = cache_usage.group_history(items)[0]
        self.assertEqual(group["count"], 49)
        self.assertEqual(group["details"], items)
        self.assertEqual(group["read"], sum(e["read"] for e in group["details"]))
        self.assertTrue(all("details" not in e for e in group["details"]))

    def test_all_openai_misses_with_zero_writes_survive_history_and_state_upgrade(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            with open(os.path.join(tmp, "cache-usage-session.json"), "w", encoding="utf-8") as handle:
                json.dump({"v": 10, "offset": 999999, "misses": 999}, handle)
            with open(transcript, "w", encoding="utf-8") as handle:
                for minute in range(25):
                    handle.write(json.dumps({"timestamp": f"2026-09-17T12:{minute:02d}:00Z",
                        "message": {"model": "gpt-a" if minute == 0 else "gpt-b", "usage": {
                            "input_tokens": 1000 + minute,
                            "cache_read_input_tokens": 100 if minute == 0 else 0,
                            "cache_creation_input_tokens": 0, "output_tokens": 10}}}) + "\n")
            with patch.object(cache_usage, "account_events", return_value=[]):
                stats = cache_usage.collect(transcript, state_dir=tmp)
        self.assertEqual(stats["misses"], 24)
        misses = [e for e in stats["history"] if e["kind"] == "miss"]
        self.assertEqual(sum(e.get("count", 1) for e in misses), 24)
        self.assertTrue(all(e["written"] == 0 for e in misses))
        self.assertEqual(misses[0]["explain"], "model")
        self.assertEqual(misses[0]["fresh"], 1001)
        self.assertEqual(misses[-1]["details"][-1]["fresh"], 1024)
        self.assertEqual(stats["explained_misses"], 1)
        self.assertEqual(stats["early_misses"], 23)

    def test_compaction_measurements_for_mood_are_never_grouped(self):
        events = [{"ts": f"2026-09-17T11:0{i}:00Z", "context_before": 220000,
                   "context_after": 70000 + i} for i in range(2)]
        history = cache_usage.build_history({}, [], [], events)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["count"], 2)
        measurements = cache_usage.build_history({}, [], [], events, group_consecutive=False)
        self.assertEqual(len(measurements), 2)


if __name__ == "__main__":
    unittest.main()
