#!/usr/bin/env python3
"""Tests for the Z.AI coding-plan quota windows (zai_usage)."""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

import account_switcher

NOW_MS = time.time() * 1000
HOUR = 3600


def fake_response(body):
    class _Response:
        def read(self):
            return json.dumps(body).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    return _Response()


def quota_body(limits):
    return {"code": 200, "msg": "Operation successful",
            "data": {"limits": limits, "level": "lite"}, "success": True}


class ZaiUsageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = os.path.join(tmp.name, "zai-usage-cache.json")
        self.config_path = os.path.join(tmp.name, "claude-custom-config.toml")
        with open(self.config_path, 'w', encoding='utf-8') as out:
            out.write('zaiUsageCacheTtlSec = 300\n')
        config_patch = mock.patch.object(account_switcher, "USAGE_CONFIG_FILE", self.config_path)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        patcher = mock.patch.object(
            account_switcher, "ZAI_USAGE_CACHE_FILE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.env = {"ANTHROPIC_BASE_URL": "https://api.z.ai",
                    "ANTHROPIC_AUTH_TOKEN": "test-key"}

    def fetch(self, body=None, error=None, force=False):
        calls = []
        def urlopen(request, timeout=None):
            calls.append(request)
            if error is not None:
                raise error
            return fake_response(body if body is not None else quota_body([
                {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                 "usage": 2000, "currentValue": 166, "remaining": 1833,
                 "percentage": 8, "nextResetTime": NOW_MS + HOUR * 1000},
                {"type": "CREDIT_LIMIT", "unit": 6, "number": 1,
                 "usage": 10000, "currentValue": 6675, "remaining": 3324,
                 "percentage": 66, "nextResetTime": NOW_MS + 3 * HOUR * 1000},
            ]))
        with mock.patch("account_switcher.urllib.request.urlopen", urlopen):
            result = account_switcher.zai_usage(self.env, force_refresh=force)
        return result, calls

    def test_parses_windows_with_claude_like_keys(self):
        result, _ = self.fetch()

        self.assertIsNotNone(result)
        windows = result["windows"]
        self.assertEqual([w["key"] for w in windows],
                         ["five_hour", "seven_day"])
        self.assertEqual([w["label"] for w in windows], ["5 ч", "7 дн"])
        self.assertEqual([w["percent"] for w in windows], [8, 66])
        self.assertTrue(all(w["resetsInSec"] > 0 for w in windows))
        self.assertFalse(any(w["expired"] for w in windows))
        self.assertEqual(result["sourceLabel"], "Данные Z.AI")

    def test_result_is_cached_and_age_grows(self):
        first, _ = self.fetch()
        second, calls = self.fetch()  # сеть больше не зовём

        self.assertEqual(len(calls), 0)
        self.assertEqual(second["windows"], first["windows"])
        self.assertIsInstance(second.get("ageSec"), int)

    def test_disabled_cache_neither_reads_nor_updates_existing_cache(self):
        self.fetch()
        with open(self.cache, 'rb') as f:
            original = f.read()
        with open(self.config_path, 'w', encoding='utf-8') as out:
            out.write('zaiUsageCacheTtlSec = 0\n')
        for _ in range(2):
            result, calls = self.fetch(body=quota_body([
                {'unit': 3, 'number': 5, 'percentage': 25, 'nextResetTime': NOW_MS + 3600000}]))
            self.assertEqual(len(calls), 1)
            self.assertEqual(result['windows'][0]['percent'], 25)
            self.assertEqual(result['windows'][0]['resetAt'], (NOW_MS + 3600000) / 1000)
            self.assertIsInstance(result['observedAt'], float)
        failed, calls = self.fetch(error=OSError('offline'))
        self.assertIsNone(failed)
        self.assertEqual(len(calls), 1)
        with open(self.cache, 'rb') as f:
            self.assertEqual(f.read(), original)

    def test_configurable_ttl_is_reloaded_without_restart(self):
        self.fetch()
        with open(self.config_path, 'w', encoding='utf-8') as out:
            out.write('zaiUsageCacheTtlSec = 10\n')
        with mock.patch('account_switcher.time.time', return_value=time.time() + 11):
            _, calls = self.fetch()
        self.assertEqual(len(calls), 1)

    def test_not_zai_env_never_touches_network(self):
        self.env["ANTHROPIC_BASE_URL"] = "https://api.example.test"

        result, calls = self.fetch()

        self.assertIsNone(result)
        self.assertEqual(len(calls), 0)

    def test_explicit_refresh_bypasses_fresh_success_and_failure_caches(self):
        self.fetch()
        fresh, calls = self.fetch(body=quota_body([
            {"unit": 3, "number": 5, "percentage": 25, "nextResetTime": NOW_MS + HOUR * 1000},
        ]), force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(fresh['windows'][0]['percent'], 25)
        account_switcher._write_zai_cache('test-key', None, self.cache, 1)
        fresh, calls = self.fetch(force=True)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(fresh)

    def test_failed_explicit_refresh_preserves_old_values_and_cache_timestamp(self):
        first, _ = self.fetch()
        with open(self.cache, encoding='utf-8') as f:
            saved = json.load(f)
        old, calls = self.fetch(error=OSError('offline'), force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(old['windows'], first['windows'])
        self.assertTrue(old['refreshFailed'])
        with open(self.cache, encoding='utf-8') as f:
            self.assertEqual(json.load(f), saved)

    def test_network_failure_is_negative_cached(self):
        first, first_calls = self.fetch(error=OSError("down"))
        second, second_calls = self.fetch(error=OSError("down"))

        self.assertIsNone(first)
        self.assertEqual(len(first_calls), 1)
        self.assertIsNone(second)
        self.assertEqual(len(second_calls), 0)  # неудача свежа — ждём

    def test_past_reset_marks_window_expired(self):
        result, _ = self.fetch(body=quota_body([
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
             "percentage": 90, "nextResetTime": NOW_MS - 1000},
        ]))

        window = result["windows"][0]
        self.assertTrue(window["expired"])
        self.assertEqual(window["percent"], 0)
        self.assertIsNone(window["resetsInSec"])

    def test_unknown_window_kind_gets_generic_label(self):
        result, _ = self.fetch(body=quota_body([
            {"type": "CREDIT_LIMIT", "unit": 4, "number": 2,
             "percentage": 10, "nextResetTime": NOW_MS + HOUR * 1000},
        ]))

        window = result["windows"][0]
        self.assertIsNone(window["key"])
        self.assertEqual(window["label"], "2 ×4")

    def test_bigmodel_station_sends_raw_key(self):
        captured = []
        def urlopen(request, timeout=None):
            captured.append(request.get_header("Authorization"))
            return fake_response(quota_body([
                {"type": "CREDIT_LIMIT", "unit": 3, "number": 5,
                 "percentage": 1, "nextResetTime": NOW_MS + HOUR * 1000},
            ]))
        self.env["ANTHROPIC_BASE_URL"] = "https://open.bigmodel.cn"

        with mock.patch("account_switcher.urllib.request.urlopen", urlopen):
            result = account_switcher.zai_usage(self.env)

        self.assertIsNotNone(result)
        self.assertEqual(captured, ["test-key"])  # без «Bearer »

    def test_empty_limits_is_negative_cached(self):
        self.fetch(body=quota_body([]))
        _, calls = self.fetch(body=quota_body([]))

        self.assertEqual(len(calls), 0)


if __name__ == "__main__":
    unittest.main()
