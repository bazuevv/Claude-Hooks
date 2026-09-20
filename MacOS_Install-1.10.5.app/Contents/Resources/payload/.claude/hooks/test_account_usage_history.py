import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import account_usage_history as history
import message_costs as costs
import message_timestamps as timing
import usage_history_store


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        root_patch = patch.object(usage_history_store, "ROOT", self.root / "user-history")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        costs._FILES.clear()
        timing._CACHE.clear()
        self.at = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0).timestamp()

    def sample(self, used=10, offset=0, account="one", filename="settings_test.json"):
        return {"account": account, "account_file": filename, "billing": "subscription",
                "captured_at": self.at + offset, "windows": [{"key": "primary", "label": "7 дн",
                "used": used, "observed_at": self.at + offset, "reset_at": self.at + 86400}]}

    def samples(self, *samples):
        data = {}
        for sample in samples:
            history._consume(data, sample)
        return list(data.values())

    def test_parallel_sessions_and_duplicate_observations_count_once(self):
        samples = self.samples(self.sample(10), self.sample(15, 10), self.sample(12, 5), self.sample(15, 10))
        row = next(iter(history.quota_days(samples).values()))["windows"][0]
        self.assertEqual(row["percent"], 5)
        self.assertEqual(row["samples"], 3)

    def test_midnight_reset_and_login_changes_are_not_false_deltas(self):
        before = self.sample(90)
        after = self.sample(5, 60)
        after["windows"][0]["reset_at"] += 86400
        tomorrow = self.sample(30, 86400)
        tomorrow["windows"][0]["reset_at"] += 2 * 86400
        days = history.quota_days(self.samples(before, after, tomorrow, self.sample(80, 30, account="two")))
        today = days[datetime.fromtimestamp(self.at).date().isoformat()]
        self.assertEqual(len(today["windows"]), 2)
        self.assertTrue(today["windows"][0]["gaps"])
        self.assertIsNone(today["windows"][0]["percent"])
        self.assertIsNone(list(days.values())[-1]["windows"][0]["percent"])

    def test_single_stale_or_expired_sample_is_not_zero_consumption(self):
        single = next(iter(history.quota_days(self.samples(self.sample())).values()))
        self.assertIsNone(single["windows"][0]["percent"])
        expired = self.sample()
        expired["windows"][0]["reset_at"] = self.at - 1
        self.assertEqual(history.quota_days(self.samples(expired)), {})

    def test_counter_regression_does_not_double_count_rebound(self):
        rows = history.quota_days(self.samples(self.sample(10), self.sample(15, 10),
                                              self.sample(12, 20), self.sample(16, 30)))
        window = next(iter(rows.values()))["windows"][0]
        self.assertEqual(window["percent"], 6)
        self.assertTrue(window["gaps"])

    def test_hourly_buckets_do_not_invent_distribution_across_hours(self):
        rows = history.quota_days(self.samples(self.sample(10), self.sample(15, 60),
                                              self.sample(20, 3600), self.sample(22, 3660)), hourly=True)
        self.assertEqual([w["windows"][0]["percent"] for w in rows.values()], [5, 7])
        self.assertEqual([w["windows"][0]["externalPercent"] for w in rows.values()], [5, 7])
        self.assertTrue(all("T" in key for key in rows))

    def test_local_and_external_increases_share_a_bucket_without_double_counting(self):
        samples = self.samples(self.sample(10), self.sample(13, 60), self.sample(17, 120))
        samples[1].update(source='claude-code', task_id='task-1', phase='progress', task_started_at=self.at)
        rows = history.quota_days(samples)
        window = next(iter(rows.values()))["windows"][0]
        self.assertEqual(window["percent"], 7)
        self.assertEqual(window["externalPercent"], 4)
        self.assertEqual(window["localPercent"], 3)
        self.assertEqual(window["observedFrom"], self.at)
        self.assertEqual(window["observedTo"], self.at + 120)

    def test_task_start_does_not_claim_previous_usage_and_polls_remain_external(self):
        samples = self.samples(self.sample(10), self.sample(20, 60), self.sample(23, 120),
                               self.sample(25, 180), self.sample(27, 240))
        for index, phase in ((1, 'start'), (2, 'progress'), (4, 'end')):
            samples[index].update(source='claude-code', task_id='task-1', phase=phase, task_started_at=self.at + 60)
        samples[3]['source'] = 'background'
        window = next(iter(history.quota_days(samples).values()))['windows'][0]
        self.assertEqual(window['percent'], 17)
        self.assertEqual(window['localPercent'], 5)
        self.assertEqual(window['externalPercent'], 12)

    def test_task_observation_without_baseline_is_not_local(self):
        samples = self.samples(self.sample(10), self.sample(20, 60))
        samples[1].update(source='claude-code', task_id='task-1', phase='end')
        window = next(iter(history.quota_days(samples).values()))['windows'][0]
        self.assertEqual(window['externalPercent'], 10)

    def test_cross_day_growth_is_retained_only_at_detection(self):
        rows = history.quota_days(self.samples(self.sample(10), self.sample(20, 15 * 3600)))
        self.assertIsNone(list(rows.values())[0]["windows"][0]["percent"])
        self.assertEqual(list(rows.values())[1]["windows"][0]["externalPercent"], 10)

    def test_real_reset_counts_only_new_period_usage(self):
        before, after = self.sample(90), self.sample(3, 6 * 3600)
        before["windows"][0].update(reset_at=self.at + 3600, duration_seconds=18000)
        after["windows"][0].update(reset_at=self.at + 7 * 3600, duration_seconds=18000)
        rows = history.quota_days(self.samples(before, after), hourly=True)
        self.assertEqual(list(rows.values())[-1]["windows"][0]["externalPercent"], 3)

    def test_calendar_months_leap_day_and_invalid_periods(self):
        period, keys = history.selected_period("month", "2024-02")
        self.assertEqual(len(keys), 29)
        self.assertEqual(keys[-1], "2024-02-29")
        self.assertEqual(len(history.selected_period("month", "2023-02")[1]), 28)
        self.assertEqual(history.selected_period("day", "2024-02-29")[1][-1], "2024-02-29T23")
        self.assertEqual(history.selected_period()[0], {"mode": "day", "date": datetime.now().date().isoformat()})
        for mode, selected in (("week", "2024-02"), ("month", "2024-13"), ("day", "2023-02-29"), ("day", "2024-1-1")):
            with self.assertRaises(ValueError):
                history.selected_period(mode, selected)

    def test_saved_history_older_than_thirty_days_is_available_by_month_and_hour(self):
        old_at = datetime(2024, 2, 29, 14).timestamp()
        for used, offset in ((10, 0), (13, 60)):
            sample = self.sample(used)
            sample["captured_at"] = old_at + offset
            sample["windows"][0].update(observed_at=old_at + offset, reset_at=old_at + 86400)
            history._save_sample(self.tmp.name, sample)
        with patch.object(history.account_switcher, "_load_account", return_value=(True, "", {})), \
                patch.object(costs, "capture", return_value=self.sample()):
            month = history.details("settings_test.json", self.tmp.name, mode="month", selected="2024-02")
            self.assertEqual(month["buckets"][28]["windows"][0]["percent"], 3)
            costs._FILES.clear()
            day = history.details("settings_test.json", self.tmp.name, selected="2024-02-29")
            self.assertEqual(day["buckets"][14]["windows"][0]["percent"], 3)
            self.assertTrue(day["buckets"][13]["missing"])

    def test_index_isolation_and_partial_jsonl_append(self):
        path = str(self.root / "chat.jsonl")
        costs.append_event(path, self.tmp.name, {"id": "u", "phase": "start", **self.sample()})
        journal = costs.events_path(path, self.tmp.name)
        self.assertIn("u", costs.observations(path, self.tmp.name))
        result = costs._read_index(journal, history._consume, cache_tag="account-history")
        self.assertEqual(len(result), 1)
        self.assertIn("u", costs.observations(path, self.tmp.name))
        with open(journal, "a", encoding="utf-8") as out:
            out.write(json.dumps({"id": "u", "phase": "end", **self.sample(20, 60)}))
        self.assertEqual(len(costs._read_index(journal, history._consume, cache_tag="account-history")), 1)
        with open(journal, "a", encoding="utf-8") as out:
            out.write("\n")
        self.assertEqual(len(costs._read_index(journal, history._consume, cache_tag="account-history")), 2)

    def test_api_history_survives_reload_deduplicates_and_excludes_other_accounts(self):
        path = str(self.root / "chat.jsonl")
        at = datetime.fromtimestamp(self.at).astimezone().isoformat()
        records = [
            {"type": "user", "uuid": "u", "timestamp": at, "message": {"content": "hi"}},
            {"type": "assistant", "uuid": "a", "timestamp": at,
             "message": {"id": "r", "model": "test", "usage": {"input_tokens": 100, "output_tokens": 10}}},
        ]
        Path(path).write_text("".join(json.dumps(r) + "\n" for r in records + [records[1]]), encoding="utf-8")
        sample = {"billing": "api", "account_file": "settings_test.json", "account": "one",
                  "captured_at": self.at, "windows": [], "rates": {"test": {"input": 2, "output": 10, "cache_read": 0.2}}}
        costs.append_event(path, self.tmp.name, {"id": "u", "phase": "start", **sample})
        with patch.object(history.account_switcher, "_load_account", return_value=(True, "", {})), \
                patch.object(costs, "capture", return_value=sample):
            data = history.details("settings_test.json", self.tmp.name)
            api = data["days"][0]["api"]
            self.assertEqual(api["requests"], 1)
            self.assertAlmostEqual(api["usd"], 0.0003)
            costs._FILES.clear()
            timing._CACHE.clear()
            self.assertEqual(history.details("settings_test.json", self.tmp.name)["days"][0]["api"], api)
            self.assertEqual(history.details("settings_other.json", self.tmp.name)["days"][0]["api"], api,
                             "Same identity under another profile shares usage history")
            with patch.object(costs, "capture", return_value={**sample, "account": "different"}):
                self.assertTrue(all("api" not in d for d in history.details("settings_test.json", self.tmp.name)["days"]))
            self.assertEqual(len(data["days"]), 30)
            self.assertTrue(data["days"][1]["missing"])
            self.assertNotIn("hi", json.dumps(data))
            self.assertEqual(len(data["buckets"]), 24)
            self.assertEqual(data["buckets"][10]["api"], api)
            self.assertTrue(data["buckets"][9]["missing"])
            month = history.details("settings_test.json", self.tmp.name, mode="month", selected=at[:7])
            self.assertEqual(month["buckets"][int(at[8:10]) - 1]["api"], api)
            old = history.details("settings_test.json", self.tmp.name, mode="month", selected="2020-02")
            self.assertEqual(len(old["buckets"]), 29)
            self.assertTrue(all(row.get("missing") for row in old["buckets"]))

    def test_unknown_api_rate_is_not_reported_as_zero(self):
        sample = {"billing": "api", "account": "one", "account_file": "settings_test.json", "captured_at": self.at, "windows": []}
        path = str(self.root / "chat.jsonl")
        costs.append_event(path, self.tmp.name, {"id": "u", "phase": "start", **sample})
        snap = {"tasks": {"u": {"cost": {"kind": "api", "usd": None, "requests": 1}}},
                "times": {"u": datetime.fromtimestamp(self.at).astimezone().isoformat()}}
        with patch.object(history.account_switcher, "_load_account", return_value=(True, "", {})), \
                patch.object(costs, "capture", return_value=sample), \
                patch.object(timing, "snapshot", return_value=snap):
            api = history.details("settings_test.json", self.tmp.name)["days"][0]["api"]
            self.assertEqual(api["unknown"], api["messages"])

    def test_invalid_filename_rejected_before_capture(self):
        with patch.object(costs, "capture") as capture:
            with self.assertRaises(ValueError):
                history.details("../settings.json", self.tmp.name)
            capture.assert_not_called()

    def test_five_hour_graph_includes_only_windows_overlapping_current_subscription(self):
        paid = {'start': '2026-09-01T00:00:00+03:00', 'end': '2026-10-01T00:00:00+03:00'}
        start = datetime.fromisoformat(paid['start']).timestamp()
        end = datetime.fromisoformat(paid['end']).timestamp()
        def cycle(key, at, seconds=18000):
            return {'id': key, 'window': 'short', 'label': '5 ч', 'start': at,
                    'end': at + seconds, 'duration': seconds, 'used': 40}
        cycles = [cycle('previous', start - 18000), cycle('overlap', start - 3600),
                  cycle('current', start + 18000), cycle('weekly', start, 604800), cycle('next', end)]
        result = history.short_usage(list(reversed(cycles)), paid)
        self.assertEqual([p['id'] for p in result], ['overlap', 'current'])
        self.assertEqual(history.short_usage(cycles, None), [])

    def test_subscription_week_uses_paid_start_and_handles_boundary_and_expiry(self):
        period = {"start": "2024-02-01T12:30:00+03:00", "end": "2024-03-01T12:30:00+03:00"}
        # Convert test hour into the local timezone used by the chart.
        moment = datetime.fromisoformat("2024-02-08T12:00:00+03:00").astimezone()
        bucket = moment.strftime("%Y-%m-%dT%H")
        weeks = history.subscription_weeks(bucket, [period, period])
        self.assertEqual([w["number"] for w in weeks], [1, 2])
        self.assertEqual(history.subscription_weeks("2024-01-01", [period]), [])
        self.assertEqual(history.subscription_weeks("2024-04-01", [period]), [])
        self.assertEqual(history.subscription_weeks("2024-02-20", [period])[0]["number"], 3)

    def test_weekly_totals_use_cumulative_counter_and_separate_accounts_and_periods(self):
        period = {"start": "2026-01-01T00:00:00+00:00", "end": "2026-02-01T00:00:00+00:00"}
        def observation(day, used, reset=8, account="one"):
            return {"account": account, "key": "primary", "label": "7 дн", "used": used,
                    "observed_at": datetime.fromisoformat(f"2026-01-{day:02}T12:00:00+00:00").timestamp(),
                    "reset_at": datetime.fromisoformat(f"2026-01-{reset:02}T00:00:00+00:00").timestamp()}
        samples = [observation(2, 37), observation(3, 39), observation(3, 39),
                   observation(3, 99, account="other"), observation(9, 40, 15)]
        weeks = history.weekly_usage(samples, period, "one")
        self.assertEqual(len(weeks), 5)
        self.assertEqual(weeks[0]["windows"][0]["percent"], 39)
        self.assertEqual(weeks[1]["windows"][0]["percent"], 40)
        self.assertEqual(weeks[2]["windows"], [])
        self.assertEqual(weeks[-1]["end"], period["end"])
        # A counter started before a paid week cannot assign its initial usage to that week.
        partial = history.weekly_usage([observation(2, 10, 5), observation(3, 20, 5), observation(8, 30, 10)], period, "one")
        self.assertEqual(partial[0]["windows"][0]["percent"], 10)
        self.assertIsNone(partial[1]["windows"][0]["percent"])
        # The reset may be after week end: its earlier observed usage is still attributable.
        crossing_end = history.weekly_usage([observation(4, 37, 10), observation(5, 39, 10)], period, "one")
        self.assertEqual(crossing_end[0]["windows"][0]["percent"], 39)
        self.assertEqual(history.weekly_usage(samples, None, "one"), [])

    def test_weekly_short_windows_add_reset_cycles_without_mixing_quotas(self):
        period = {"start": "2026-01-01T00:00:00+00:00", "end": "2026-01-08T00:00:00+00:00"}
        base = datetime.fromisoformat(period["start"]).timestamp()
        samples = [{"account": "one", "key": "short", "label": "5 ч", "used": used,
                    "observed_at": base + hour * 3600, "reset_at": base + reset * 3600}
                   for hour, used, reset in [(1, 35, 5), (2, 50, 5), (6, 20, 10)]]
        samples.append({"account": "one", "key": "week", "label": "7 дн", "used": 9,
                        "observed_at": base + 7200, "reset_at": base + 7 * 86400})
        windows = history.weekly_usage(samples, period, "one")[0]["windows"]
        self.assertEqual({w["key"]: w["percent"] for w in windows}, {"short": 70, "week": 9})

    def test_subscription_period_persists_when_provider_no_longer_returns_it(self):
        today = datetime.now().date()
        period = {"start": today.isoformat() + "T00:00:00+03:00", "end": (today.replace(year=today.year + 1)).isoformat() + "T00:00:00+03:00"}
        with patch.object(history.account_switcher, "_load_account", return_value=(True, "", {})), \
                patch.object(costs, "capture", return_value=self.sample()), \
                patch.object(history, "subscription_period", return_value=period) as read_period:
            first = history.details("settings_test.json", self.tmp.name)
            self.assertTrue(any(row.get("subscriptionWeeks") for row in first["buckets"]))
            other_view = history.details("settings_test.json", self.tmp.name, mode="month", selected="2020-01")
            self.assertEqual(first["weeklyUsage"], other_view["weeklyUsage"],
                             "Current paid-week totals do not follow the lower chart's selected month")
            costs._FILES.clear()
            read_period.return_value = None
            second = history.details("settings_test.json", self.tmp.name)
            self.assertEqual([r.get("subscriptionWeeks") for r in first["buckets"]],
                             [r.get("subscriptionWeeks") for r in second["buckets"]])

    def test_saved_subscription_dates_without_timezone_are_readable(self):
        period = {"start": datetime.fromtimestamp(self.at - 86400).isoformat(),
                  "end": datetime.fromtimestamp(self.at + 86400).isoformat()}
        history._save_sample(self.tmp.name, {**self.sample(), "subscriptionPeriod": period})
        with patch.object(history.account_switcher, "_load_account", return_value=(True, "", {})), \
                patch.object(costs, "capture", return_value=self.sample()), \
                patch.object(history, "subscription_period", return_value=None):
            result = history.details("settings_test.json", self.tmp.name)
        self.assertTrue(result["ok"])
        self.assertIsNotNone(datetime.fromisoformat(result["subscriptionPeriod"]["start"]).tzinfo)


if __name__ == "__main__":
    unittest.main()
