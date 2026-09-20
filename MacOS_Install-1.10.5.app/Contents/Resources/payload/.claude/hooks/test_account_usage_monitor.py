import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import account_usage_monitor as monitor
import usage_history_store


class BackgroundUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sample = {"billing": "subscription", "provider": "openai", "account": "test-id",
                       "account_file": "settings_openai.json", "captured_at": 10000,
                       "windows": [{"key": "primary", "label": "7 d", "duration_seconds": 604800,
                                    "used": 10, "observed_at": 10000, "reset_at": 700000}]}
        monitor._seen.clear()
        monitor._attempts.clear()
        for target, name, value in (
            (usage_history_store, "ROOT", self.root / "history"),
            (monitor, "settings", (True, 3600)),
            (monitor.glob, "glob", [str(self.root / "settings_openai.json")]),
            (monitor.account_switcher, "_describe", {"provider": "openai"}),
            (monitor.account_switcher, "_read_env", {}),
            (monitor.account_usage_history, "subscription_period", None),
        ):
            patcher = patch.object(target, name, value) if name == "ROOT" else patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        log = patch.object(monitor.hook_log, "log")
        log.start()
        self.addCleanup(log.stop)

    def test_disabled_never_contacts_provider(self):
        monitor.settings.return_value = (False, 3600)
        with patch.object(monitor.message_costs, "capture") as capture:
            monitor.poll(self.root, now=10000)
            capture.assert_not_called()

    def test_hourly_poll_defers_to_task_observations_in_database(self):
        with patch.object(monitor.message_costs, "capture", return_value=self.sample) as capture:
            monitor.poll(self.root, now=10000)
            self.assertEqual(capture.call_count, 1)
            monitor.poll(self.root, now=13599)
            self.assertEqual(capture.call_count, 1)
            # Another process (message hook) records a new observation.
            fresh = {**self.sample, "windows": [{**self.sample["windows"][0], "observed_at": 13000}]}
            usage_history_store.record_account(self.root, fresh)
            monitor.poll(self.root, now=14000)
            self.assertEqual(capture.call_count, 1)
            monitor.poll(self.root, now=16600)
            self.assertEqual(capture.call_count, 2)

    def test_failures_are_rate_limited_and_do_not_stop_monitor(self):
        with patch.object(monitor.message_costs, "capture", side_effect=OSError) as capture:
            monitor.poll(self.root, now=10000)
            monitor.poll(self.root, now=10030)
            self.assertEqual(capture.call_count, 1)
            monitor.poll(self.root, now=13600)
            self.assertEqual(capture.call_count, 2)

    def test_accs_reuses_snapshot_and_preserves_paid_period(self):
        snapshot = {"rateLimits": {}}
        entry = {"file": "settings_openai.json", "subscription": {
            "paidAt": "2026-09-01T00:00:00", "until": "2026-10-01T00:00:00"}}
        with patch.object(monitor.message_costs, "capture", return_value=self.sample) as capture, \
                patch.object(usage_history_store, "record_account") as record:
            monitor.record_entry(entry, {"openai_snapshot": snapshot}, self.root)
            capture.assert_called_once_with("settings_openai.json", openai_snapshot=snapshot)
            expected = monitor.datetime.fromisoformat(entry["subscription"]["until"]).astimezone().isoformat()
            self.assertEqual(record.call_args.args[1]["subscriptionPeriod"]["end"], expected)
        self.assertEqual(monitor.last_observation("settings_openai.json"), 10000)

    def test_supplied_openai_response_does_not_fetch_again_even_when_empty(self):
        snapshot = {"account": {"type": "chatgpt", "accountId": "example-id"},
                    "rateLimits": {"primary": {"usedPercent": 12, "resetsAt": 700000,
                                                 "windowDurationMins": 10080}}}
        with patch.object(monitor.account_switcher.codex_bridge_manager, "account_snapshot") as fetch:
            sample = monitor.message_costs.capture("settings_openai.json", openai_snapshot=snapshot)
            self.assertEqual(sample["windows"][0]["used"], 12)
            self.assertEqual(monitor.message_costs.capture("settings_openai.json", openai_snapshot={})["windows"], [])
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
