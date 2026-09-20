import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import message_costs as costs
import message_timestamps as timing
import usage_history_store


RATES = {"claude-test": {"input": 2, "output": 10, "cache_read": 0.2,
                         "cache_write_5m": 2.5, "cache_write_1h": 4}}


def sample(at=1000, used=10, account="one"):
    return {"billing": "subscription", "account": account, "captured_at": at,
            "windows": [{"key": "primary", "label": "5 h", "used": used,
                         "reset_at": 10000, "observed_at": at}], "rates": {}}


class MessageCostsTests(unittest.TestCase):
    def setUp(self):
        costs._FILES.clear()
        costs._REFRESHING.clear()
        costs._RETRY_AT.clear()
        timing._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root_patch = patch.object(usage_history_store, "ROOT", Path(self.tmp.name) / "user-history")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        log_patch = patch("hook_log.log")
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.path = Path(self.tmp.name) / "chat.jsonl"
        self.config = patch.object(costs, "config", return_value={"enabled": True, "api_rates": RATES})
        self.config.start()
        self.addCleanup(self.config.stop)

    def append(self, *records, path=None):
        with (path or self.path).open("a", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")

    def test_hook_events_persist_source_and_link_to_original_message(self):
        for phase, at, used in (('start', 1000, 10), ('progress', 1030, 12), ('end', 1060, 13)):
            costs.append_event(str(self.path), self.tmp.name, {'id': 'one-message', 'phase': phase, **sample(at, used)})
        events = costs.observations(str(self.path), self.tmp.name)['one-message']
        self.assertEqual({e['source'] for e in events.values()}, {'claude-code'})
        self.assertEqual(len({e['task_id'] for e in events.values()}), 1)
        self.assertEqual({e['task_started_at'] for e in events.values()}, {1000})
        # Switching account mid-task must not attach the previous account baseline.
        costs.append_event(str(self.path), self.tmp.name, {'id': 'one-message', 'phase': 'end', **sample(1100, 30, 'two')})
        self.assertIsNone(costs.observations(str(self.path), self.tmp.name)['one-message']['end']['task_started_at'])

    def test_hook_settings_and_api_rates_are_loaded_from_separate_files(self):
        self.config.stop()
        hooks = Path(self.tmp.name) / 'claude-custom-config.toml'
        rates = Path(self.tmp.name) / 'message-costs.toml'
        hooks.write_text('messageCostsEnabled = false\nmessageCostsRefreshIntervalSec = 17\n'
                         'messageCostAccounts = { "account.json" = { billing = "subscription", history_id = "stable" } }\n',
                         encoding='utf-8')
        rates.write_text('enabled = true\nrefresh_interval_seconds = 999\n'
                         '[api_rates.model]\ninput = 2\n'
                         '[accounts."account.json"]\nbilling = "api"\nhistory_id = "obsolete"\n'
                         '[accounts."account.json".api_rates.model]\ninput = 3\n', encoding='utf-8')
        with patch.object(costs, 'CONFIG_PATH', rates), patch.object(costs, 'HOOK_CONFIG_PATH', hooks):
            cfg = costs.config()
            self.assertFalse(cfg['enabled'])
            self.assertEqual(costs.refresh_interval(), 17)
            self.assertEqual(cfg['api_rates']['model']['input'], 2)
            self.assertEqual(cfg['accounts']['account.json'], {
                'billing': 'subscription', 'history_id': 'stable', 'api_rates': {'model': {'input': 3}}})
            hooks.write_text('messageCostsEnabled = true\nmessageCostsRefreshIntervalSec = 0\n', encoding='utf-8')
            self.assertTrue(costs.config()['enabled'])
            self.assertEqual(costs.refresh_interval(), 0)
            hooks.unlink()
            self.assertEqual(costs.config()['api_rates']['model']['input'], 2)

    def user(self, key, text="hello", second=0):
        return {"type": "user", "uuid": key, "timestamp": f"2020-01-01T10:00:{second:02}Z",
                "message": {"content": text}}

    def reply(self, key, amount=100, model="claude-test", **extra):
        return {"type": "assistant", "uuid": key, "timestamp": "2020-01-01T10:00:10Z",
                "message": {"id": key, "model": model, "stop_reason": "end_turn",
                            "usage": {"input_tokens": amount, "output_tokens": 10}}, **extra}

    def start_api(self, key):
        with patch.object(costs, "capture", return_value={"billing": "api", "rates": RATES}):
            costs.record_start(str(self.path), self.tmp.name, key)

    def data(self):
        return timing.snapshot(str(self.path), self.tmp.name, include_messages=True)

    def test_api_deduplicates_streamed_requests_and_survives_reload(self):
        self.append(self.user("first"), self.reply("r1"), self.reply("r1", 150))
        self.start_api("first")
        cost = self.data()["tasks"]["first"]["cost"]
        self.assertAlmostEqual(cost["usd"], 0.0004)
        self.assertEqual(cost["requests"], 1)
        costs._FILES.clear()
        timing._CACHE.clear()
        self.assertEqual(self.data()["messages"][0]["cost"], cost)

    def test_requests_and_subagents_are_attributed_without_double_charging(self):
        self.append(self.user("first"), self.reply("r1"))
        self.append({"type": "user", "uuid": "tool", "timestamp": "2020-01-01T10:00:11Z",
                     "sourceToolAssistantUUID": "r1", "toolUseResult": {"agentId": "abc"},
                     "message": {"content": [{"type": "tool_result"}]}})
        self.append(self.user("second", second=20), self.reply("r2", 200))
        child = self.path.with_suffix("") / "subagents" / "agent-abc.jsonl"
        child.parent.mkdir(parents=True)
        self.append(self.reply("child", 300, isSidechain=True), path=child)
        self.start_api("first")
        self.start_api("second")
        data = self.data()["tasks"]
        self.assertAlmostEqual(data["first"]["cost"]["usd"], 0.001)
        self.assertAlmostEqual(data["second"]["cost"]["usd"], 0.0005)
        self.assertEqual(data["first"]["cost"]["requests"], 2)

    def test_cache_read_and_both_write_ttls_are_priced_separately(self):
        requests = {}
        record = self.reply("r1", 1000000)
        record["message"]["usage"].update({"cache_read_input_tokens": 1000000,
            "cache_creation_input_tokens": 2000000, "cache_creation": {
                "ephemeral_1h_input_tokens": 1000000, "ephemeral_5m_input_tokens": 1000000}})
        costs.add_request(requests, record, "first")
        self.assertAlmostEqual(costs.api_cost(list(requests.values()), RATES)["usd"], 8.7001)
        record["message"]["usage"]["cache_creation"] = {}
        costs.add_request(requests, record, "first")
        self.assertIsNone(costs.api_cost(list(requests.values()), RATES)["usd"])

    def test_unknown_models_fast_and_no_usage_do_not_look_free(self):
        requests = {}
        costs.add_request(requests, self.reply("r1", model="unpriced"), "first")
        self.assertIsNone(costs.api_cost(list(requests.values()), RATES)["usd"])
        requests.clear()
        record = self.reply("r2")
        record["message"]["usage"]["speed"] = "fast"
        costs.add_request(requests, record, "first")
        self.assertIsNone(costs.api_cost(list(requests.values()), RATES)["usd"])
        self.assertIsNone(costs.api_cost([], RATES)["usd"])

    def test_subscription_delta_is_persisted_and_never_rendered_as_dollars(self):
        self.append(self.user("first"))
        with patch.object(costs, "capture", return_value=sample()):
            costs.record_start(str(self.path), self.tmp.name, "first")
        with patch.object(costs, "capture", return_value=sample(1100, 12)):
            timing.record_stop(str(self.path), self.tmp.name, completed_at="2020-01-01T10:01:00Z")
        costs._FILES.clear()
        timing._CACHE.clear()
        with patch.object(costs, "capture", side_effect=AssertionError("render must not query providers")):
            result = self.data()["tasks"]["first"]["cost"]
        self.assertEqual(result["windows"][0]["percent"], 2)
        self.assertNotIn("usd", result)
        self.assertTrue(result["approximate"])

    def test_zai_final_sample_exposes_absolute_account_usage_without_network(self):
        self.append(self.user('first'))
        start = {**sample(1000, 6), 'provider': 'zai', 'account_file': 'settings_zai.json'}
        end = {**sample(1030, 8), 'provider': 'zai', 'account_file': 'settings_zai.json'}
        start['windows'][0]['key'] = end['windows'][0]['key'] = 'five_hour'
        with patch.object(costs, 'capture', return_value=start):
            costs.record_start(str(self.path), self.tmp.name, 'first')
        with patch.object(costs, 'capture', return_value=end):
            costs.record_end(str(self.path), self.tmp.name, ['first'])
        with patch.object(costs, 'capture', side_effect=AssertionError('No provider fetch on render')):
            task = self.data()['tasks']['first']
        self.assertEqual(task['cost']['windows'][0]['percent'], 2)
        self.assertEqual(task['account_usage']['file'], 'settings_zai.json')
        self.assertEqual(task['account_usage']['usage']['windows'][0]['percent'], 8)
        self.assertEqual(task['account_usage']['usage']['observedAt'], 1030)

    def test_all_subscription_providers_expose_latest_task_sample_without_fetching(self):
        for provider in ('openai', 'anthropic', 'custom'):
            with self.subTest(provider=provider):
                message_id = provider + '-message'
                filename = 'settings_' + provider + '.json'
                self.append(self.user(message_id))
                for phase, at, used in (('start', 1000, 60), ('progress', 1030, 61), ('end', 1060, 62)):
                    costs.append_event(str(self.path), self.tmp.name, {'id': message_id, 'phase': phase,
                                       **sample(at, used), 'provider': provider, 'account_file': filename})
                    with patch.object(costs, 'capture', side_effect=AssertionError('No fetch for cached task usage')):
                        update = self.data()['tasks'][message_id]['account_usage']
                    self.assertEqual(update['file'], filename)
                    self.assertEqual(update['usage']['observedAt'], at)
                    self.assertEqual(update['usage']['windows'][0]['percent'], used)
                    self.assertEqual(update['usage']['windows'][0]['resetAt'], 10000)
                    self.assertEqual(update['usage']['windows'][0]['key'], 'openai_primary' if provider == 'openai' else 'primary')

    def test_reset_stale_and_changed_account_are_not_false_zero(self):
        before = sample()
        for after, reason in ((sample(1100, 5), "reset"), (sample(1100, 12, "other"), "account_changed")):
            result = costs.subscription_cost(before, after)
            self.assertEqual(result["reason"], reason)
            self.assertEqual(result["windows"], [])
        after = sample(1100, 12)
        after["windows"][0]["observed_at"] = 1000
        self.assertEqual(costs.subscription_cost(before, after)["reason"], "stale")
        after = sample(1100, 10)
        self.assertEqual(costs.subscription_cost(before, after)["windows"][0]["percent"], 0)

    def test_submit_before_transcript_is_bound_by_prompt_and_time_after_reload(self):
        at = 1577872800  # 2020-01-01T10:00:00Z
        with patch.object(costs.time, "time", return_value=at), patch.object(costs, "capture", return_value=sample(at)):
            costs.record_submission(str(self.path), self.tmp.name, "private prompt")
        self.assertFalse(self.path.exists())
        self.append(self.user("old", "private prompt"))
        # A later repeated prompt must not acquire the older message's baseline.
        self.append(self.user("new", "private prompt", second=30))
        costs._FILES.clear()
        result = self.data()["tasks"]
        self.assertEqual(result["old"]["cost"]["reason"], "pending")
        self.assertEqual(result["new"]["cost"]["reason"], "no_baseline")
        saved = Path(costs.events_path(str(self.path), self.tmp.name)).read_text(encoding="utf-8")
        self.assertNotIn("private prompt", saved)
        costs._FILES.clear()
        self.assertEqual(self.data()["tasks"], result)

    def test_historical_message_has_no_invented_billing_and_partial_json_is_retried(self):
        self.append(self.user("first"))
        self.assertEqual(self.data()["tasks"]["first"]["cost"]["reason"], "no_baseline")
        event = json.dumps({"id": "first", "phase": "start", "billing": "api", "rates": RATES})
        path = Path(costs.events_path(str(self.path), self.tmp.name))
        path.write_text(event[:20], encoding="utf-8")
        self.assertEqual(costs.observations(str(self.path), self.tmp.name), {})
        with path.open("a", encoding="utf-8") as f:
            f.write(event[20:] + "\n")
        self.assertEqual(costs.observations(str(self.path), self.tmp.name)["first"]["start"]["billing"], "api")

    def test_openai_capture_preserves_reported_window_and_hides_identity(self):
        account = costs.account_switcher
        with patch.object(account, "get_current_account", return_value="settings_openai.json"), \
                patch.object(account, "_read_env", return_value={"ANTHROPIC_AUTH_TOKEN": "private-token"}), \
                patch.object(account, "_describe", return_value={"provider": "openai", "oauth": False}), \
                patch.object(account.codex_bridge_manager, "account_snapshot", return_value={
                    "account": {"type": "chatgpt", "email": "private@example.test"},
                    "rateLimits": {"primary": {"usedPercent": 35, "resetsAt": 20000, "windowDurationMins": 10080}},
                }):
            result = costs.capture()
        self.assertEqual(result["billing"], "subscription")
        self.assertEqual(result["windows"][0]["label"], "7 дн")
        self.assertEqual(result["windows"][0]["used"], 35)
        self.assertNotIn("private", json.dumps(result))

    def test_account_identity_survives_profile_and_plan_changes_but_not_login_changes(self):
        account = costs.account_switcher
        snapshot = {"account": {"type": "chatgpt", "email": "one@example.test", "planType": "plus"}}
        with patch.object(account, "_read_env", return_value={}), \
                patch.object(account, "_describe", return_value={"provider": "openai", "oauth": False}), \
                patch.object(account.codex_bridge_manager, "account_snapshot", return_value=snapshot), \
                patch.object(costs, "config", return_value={}):
            first = costs.capture("settings_one.json")
            snapshot["account"]["planType"] = "pro"
            second = costs.capture("settings_renamed.json")
            self.assertEqual(first["account"], second["account"])
            snapshot["account"]["email"] = "two@example.test"
            self.assertNotEqual(first["account"], costs.capture("settings_one.json")["account"])
            snapshot["account"] = {"type": "apiKey"}
            self.assertIsNone(costs.capture("settings_one.json")["account"])
            with patch.object(costs, "config", return_value={"accounts": {"settings_one.json": {"history_id": "my-api"}}}):
                explicit = costs.capture("settings_one.json")
                self.assertTrue(explicit["account"])
                self.assertEqual(explicit["legacy_accounts"], [])

    def test_subscription_refresh_is_async_throttled_and_never_overrides_stop(self):
        self.append(self.user("first"))
        with patch.object(costs, "capture", return_value=sample()):
            costs.record_start(str(self.path), self.tmp.name, "first")
        queued = []
        def worker(**kwargs):
            return SimpleNamespace(start=lambda: queued.append(kwargs["target"]))
        with patch.object(costs.threading, "Thread", side_effect=worker), \
                patch.object(costs.time, "time", return_value=1029) as clock, \
                patch.object(costs, "capture", return_value=sample(1030, 11)) as capture:
            initial = timing.snapshot(str(self.path), self.tmp.name, running=True)
            self.assertEqual(initial['tasks']['first']['cost_refresh'], {'next_at': 1030, 'updating': False})
            self.assertEqual(queued, [])
            clock.return_value = 1030
            for _ in range(3):
                timing.snapshot(str(self.path), self.tmp.name, running=True)
            self.assertEqual(len(queued), 1)
            self.assertTrue(timing.snapshot(str(self.path), self.tmp.name, running=True)['tasks']['first']['cost_refresh']['updating'])
            capture.assert_not_called()  # No network request inside the HTTP handler.
            queued.pop()()
            progress = self.data()["tasks"]["first"]["cost"]
            self.assertEqual(progress["windows"][0]["percent"], 1)
            self.assertNotIn("completed_at", self.data()["tasks"]["first"])
            costs._FILES.clear()
            self.assertEqual(self.data()["tasks"]["first"]["cost"], progress)
            self.assertEqual(timing.snapshot(str(self.path), self.tmp.name, running=True)['tasks']['first']['cost_refresh']['next_at'], 1060)
            with patch.object(costs, 'config', return_value={'refresh_interval_seconds': 0}):
                self.assertNotIn('cost_refresh', timing.snapshot(str(self.path), self.tmp.name, running=True)['tasks']['first'])
            clock.return_value = 1059
            timing.snapshot(str(self.path), self.tmp.name, running=True)
            self.assertEqual(queued, [])
            clock.return_value = 1060
            timing.snapshot(str(self.path), self.tmp.name, running=True)
            self.assertEqual(len(queued), 1)
            capture.return_value = sample(1060, 12)
            timing.record_stop(str(self.path), self.tmp.name, completed_at="2020-01-01T10:01:00Z")
            capture.return_value = sample(1061, 15)
            queued.pop()()
            self.assertEqual(self.data()["tasks"]["first"]["cost"]["windows"][0]["percent"], 2)
            self.assertNotIn('cost_refresh', timing.snapshot(str(self.path), self.tmp.name, running=True)['tasks']['first'])

    def test_api_refresh_obeys_interval_and_final_cost_updates_immediately(self):
        self.append(self.user("first"), self.reply("r1", 100))
        self.start_api("first")
        with patch.object(costs.time, "time", return_value=1000) as clock:
            first = timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"]
            self.append(self.reply("r2", 200))
            clock.return_value = 1029
            self.assertEqual(timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"], first)
            costs._FILES.clear()
            clock.return_value = 1030
            updated = timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"]
            self.assertAlmostEqual(updated["usd"], 0.0008)
            self.append(self.reply("r3", 300))
            clock.return_value = 1031
            final = timing.record_stop(str(self.path), self.tmp.name, completed_at="2020-01-01T10:01:00Z")
            self.assertAlmostEqual(final["tasks"]["first"]["cost"]["usd"], 0.0015)

    def test_refresh_setting_zero_disables_periodic_samples_and_changes_apply_live(self):
        self.append(self.user("first"), self.reply("r1"))
        self.start_api("first")
        with patch.object(costs, "config", return_value={"refresh_interval_seconds": 0}), \
                patch.object(costs.time, "time", return_value=1000) as clock:
            first = timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"]
            self.append(self.reply("r2"))
            clock.return_value = 1200
            self.assertEqual(timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"], first)
        with patch.object(costs, "config", return_value={"refresh_interval_seconds": 10}), \
                patch.object(costs.time, "time", return_value=1200):
            updated = timing.snapshot(str(self.path), self.tmp.name, running=True)["tasks"]["first"]["cost"]
            self.assertGreater(updated["usd"], first["usd"])
        for value in (-5, "bad", True, float("nan")):
            with patch.object(costs, "config", return_value={"refresh_interval_seconds": value}):
                self.assertEqual(costs.refresh_interval(), 30)

    def test_zai_requests_fresh_quota_and_keeps_absolute_deadline_despite_network_delay(self):
        account = costs.account_switcher
        quota = {'observedAt': 1003, 'ageSec': 0, 'windows': [
            {'key': 'five_hour', 'label': '5 ч', 'percent': 10, 'resetAt': 19000, 'resetsInSec': 17997}]}
        with patch.object(account, '_read_env', return_value={'ANTHROPIC_BASE_URL': 'https://api.z.ai/api/coding/anthropic'}), \
                patch.object(account, '_describe', return_value={'provider': 'zai', 'oauth': False}), \
                patch.object(account, 'zai_usage', return_value=quota) as fetch, \
                patch.object(costs.time, 'time', side_effect=[1000, 1004]):
            result = costs.capture('settings_zai.json')
        self.assertTrue(fetch.call_args.kwargs['force_refresh'])
        self.assertEqual(result['windows'][0]['reset_at'], 19000)
        self.assertEqual(result['windows'][0]['observed_at'], 1003)

    def test_zai_message_cost_uses_only_five_hour_quota(self):
        start = {**sample(), 'provider': 'zai', 'windows': [
            {'key': 'five_hour', 'label': '5 ч', 'used': 10, 'observed_at': 1000, 'reset_at': 19000},
            {'key': 'seven_day', 'label': '7 дн', 'used': 20, 'observed_at': 1000, 'reset_at': 605800}]}
        end = {**start, 'captured_at': 1030, 'windows': [
            {**start['windows'][0], 'used': 12, 'observed_at': 1030},
            {**start['windows'][1], 'used': 23, 'observed_at': 1030}]}
        self.assertEqual(costs.subscription_cost(start, end)['windows'], [{'label': '5 ч', 'percent': 2}])
        end['windows'] = end['windows'][1:]
        self.assertEqual(costs.subscription_cost(start, end)['windows'], [])

    def test_zai_preserves_window_without_reset_and_uses_cached_observation_time(self):
        quota = {"ageSec": 30, "windows": [
            {"key": "five_hour", "label": "5 ч", "percent": 0, "resetsInSec": None, "expired": True},
            {"key": "seven_day", "label": "7 дн", "percent": 12, "resetsInSec": 100},
        ]}
        with patch.object(costs.account_switcher, "get_current_account", return_value="settings_zai.json"), \
                patch.object(costs.account_switcher, "_read_env", return_value={"ANTHROPIC_BASE_URL": "https://api.z.ai/api/coding/anthropic"}), \
                patch.object(costs.account_switcher, "_describe", return_value={"provider": "zai", "oauth": False}), \
                patch.object(costs.account_switcher, "zai_usage", return_value=quota), \
                patch.object(costs.time, "time", return_value=1000):
            result = costs.capture()
        self.assertEqual([w["key"] for w in result["windows"]], ["five_hour", "seven_day"])
        self.assertIsNone(result["windows"][0]["reset_at"])
        self.assertTrue(result["windows"][0]["expired"])
        self.assertEqual(result["windows"][1]["reset_at"], 1070)
        self.assertEqual(result["windows"][1]["observed_at"], 970)
        unknown = {**result, "windows": result["windows"][:1]}
        self.assertEqual(costs.subscription_cost(unknown, unknown)["windows"], [])


if __name__ == "__main__":
    unittest.main()
