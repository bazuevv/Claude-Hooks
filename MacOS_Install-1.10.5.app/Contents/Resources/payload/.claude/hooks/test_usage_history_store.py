import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import message_costs
import account_usage_history
import usage_history_store as store


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / ".claude" / "hooks-runtime"
        self.runtime.mkdir(parents=True)
        root_patch = patch.object(store, "ROOT", Path(self.tmp.name) / "user" / ".claude" / "usage-history")
        root_patch.start()
        self.addCleanup(root_patch.stop)
        store._MIGRATED.clear()

    def test_migration_survives_runtime_cleanup_and_restart_without_duplicates(self):
        legacy = self.runtime / "message-costs-test.jsonl"
        legacy.write_bytes(b'{"captured_at":1}\n')
        root = store.prepare(self.runtime)
        self.assertEqual(root, store.ROOT)
        path = root / legacy.name
        self.assertEqual(path.read_bytes(), legacy.read_bytes())
        store.append(self.runtime, legacy.name, b'{"captured_at":2}\n')
        store._MIGRATED.clear()
        store.prepare(self.runtime)
        self.assertEqual(len(path.read_bytes().splitlines()), 2)
        legacy.unlink()
        self.runtime.rmdir()
        store.prepare(self.runtime)
        self.assertEqual(len(path.read_bytes().splitlines()), 2)
        self.assertFalse(self.runtime.exists())

    def test_partial_legacy_record_is_retried_when_completed(self):
        legacy = self.runtime / "message-costs-test.jsonl"
        legacy.write_bytes(b'{"id":"old"}\n{"id":')
        root = store.prepare(self.runtime)
        target = root / legacy.name
        self.assertEqual(target.read_bytes(), b'{"id":"old"}\n')
        with legacy.open("ab") as output:
            output.write(b'"later"}\n')
        store.prepare(self.runtime)
        self.assertEqual(target.read_bytes().splitlines(), [b'{"id":"old"}', b'{"id":"later"}'])

    def test_migration_keeps_newer_records_last_and_preserves_original(self):
        name = "message-costs-test.jsonl"
        legacy = self.runtime / name
        legacy.write_bytes(b'{"id":"u","used":10}\n')
        root = store.directory(self.runtime)
        root.mkdir(parents=True)
        (root / name).write_bytes(b'{"id":"u","used":20}\n')
        store.prepare(self.runtime)
        self.assertEqual((root / name).read_bytes(), b'{"id":"u","used":10}\n{"id":"u","used":20}\n')
        self.assertEqual(legacy.read_bytes(), b'{"id":"u","used":10}\n')

    def test_all_billing_writers_use_persistent_directory(self):
        transcript = str(Path(self.tmp.name) / "chat.jsonl")
        message_costs.append_event(transcript, self.runtime, {"id": "u", "phase": "start"})
        account_usage_history._save_sample(self.runtime, {"captured_at": 1})
        root = store.directory(self.runtime)
        self.assertEqual(len(list(root.glob("*.jsonl"))), 1)
        self.assertTrue((root / "quota-periods.sqlite3").exists())
        self.assertEqual(list(self.runtime.glob("*.jsonl")), [])
        self.assertIn("u", message_costs.observations(transcript, self.runtime))

    def test_project_archives_merge_into_one_user_archive(self):
        name = "account-usage-samples.jsonl"
        former = self.runtime.with_name("usage-history")
        former.mkdir()
        def sample(used, at):
            return (json.dumps({"account": "one", "billing": "subscription", "windows": [
                {"key": "weekly", "label": "7 дн", "used": used, "observed_at": at,
                 "reset_at": 604800} ]}) + "\n").encode()
        original = sample(10, 1)
        (former / name).write_bytes(original)
        other = Path(self.tmp.name) / "other-project" / ".claude" / "hooks-runtime"
        other.mkdir(parents=True)
        (other / name).write_bytes(sample(20, 2))
        first = store.prepare(self.runtime)
        second = store.prepare(other)
        self.assertEqual(first, second)
        self.assertFalse((first / name).exists())
        periods = store.quota_periods.cycles(first, "one")
        self.assertEqual([s['used'] for s in periods[0]['samples']], [10, 20])
        store._MIGRATED.clear()
        store.prepare(self.runtime)
        store.prepare(other)
        self.assertEqual(store.quota_periods.cycles(first, "one"), periods)
        self.assertEqual((former / name).read_bytes(), original)

    def test_old_global_journal_is_imported_then_retired_without_data_loss(self):
        root = store.directory(self.runtime)
        root.mkdir(parents=True)
        path = root / "account-usage-samples.jsonl"
        original = (json.dumps({"account": "one", "billing": "subscription", "windows": [
            {"key": "week", "label": "7 дн", "used": 38, "observed_at": 100, "reset_at": 604800}],
            "rates": {"obsolete": 9}}) + "\n").encode()
        path.write_bytes(original)
        store.prepare(self.runtime)
        self.assertFalse(path.exists())
        self.assertEqual(next((root / "legacy").glob("*.jsonl")).read_bytes(), original)
        periods = store.quota_periods.cycles(root, "one")
        store.prepare(self.runtime)
        self.assertEqual(store.quota_periods.cycles(root, "one"), periods)


if __name__ == "__main__":
    unittest.main()
