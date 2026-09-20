import json
import tempfile
import unittest
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import account_usage_history as history
import quota_periods as quotas


class QuotaPeriodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.start = datetime(2026, 9, 1, 12).timestamp()
        self.week = 7 * 86400

    def event(self, used, elapsed, end=None, account="one", key="weekly", seconds=None):
        seconds = seconds or self.week
        return {"billing": "subscription", "provider": "test", "account": account,
                "account_file": "same-profile.json", "windows": [{
                    "key": key, "label": "quota", "used": used,
                    "duration_seconds": seconds, "observed_at": self.start + elapsed,
                    "reset_at": self.start + (seconds if end is None else end)}]}

    def ingest(self, *events):
        quotas.ingest(self.root, ("\n".join(json.dumps(e) for e in events) + "\n").encode())

    def test_source_survives_compaction_and_plain_snapshot_reimport(self):
        start = {**self.event(10, 60), 'transcript': 'session.jsonl', 'id': 'message-1', 'phase': 'start',
                 'captured_at': self.start + 60}
        progress = {**self.event(13, 120), 'transcript': 'session.jsonl', 'id': 'message-1', 'phase': 'progress'}
        self.ingest(quotas.task_source(start), quotas.task_source(progress, start))
        self.ingest(self.event(13, 120))  # Plain legacy duplicate cannot erase provenance.
        self.ingest(quotas.task_source({**self.event(13, 120), 'transcript': 'other.jsonl',
                                       'id': 'new-task', 'phase': 'start', 'captured_at': self.start + 120}))
        for elapsed in (130, 140):
            self.ingest(quotas.task_source({**progress, **self.event(13, elapsed)}, start))
        samples = quotas.cycles(self.root, 'one')[0]['samples']
        self.assertTrue(all(s['source'] == 'claude-code' for s in samples))
        self.assertEqual(len({s['task_id'] for s in samples}), 1)
        self.assertEqual(samples[-1]['phase'], 'progress')
        with closing(quotas.connect(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quota_samples').fetchone()[0], 2)

    def test_migration_discards_manual_marks_and_keeps_observations(self):
        self.ingest(self.event(10, 60))
        with closing(sqlite3.connect(self.root / 'quota-periods.sqlite3')) as db, db:
            db.execute('CREATE TABLE quota_attributions (account TEXT, start REAL, end REAL, source TEXT)')
            db.execute("INSERT INTO quota_attributions VALUES ('one',0,9999999999,'user-confirmed-local')")
            db.execute('PRAGMA user_version=3')
        samples = quotas.cycles(self.root, 'one')[0]['samples']
        self.assertEqual(samples[0]['source'], 'external')
        self.assertEqual(samples[0]['used'], 10)
        with closing(quotas.connect(self.root)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='quota_attributions'").fetchone())

    def test_legacy_hook_journal_import_has_provenance_but_polling_does_not(self):
        events = [{**self.event(used, at), 'transcript': 'chat.jsonl', 'id': 'message-1',
                   'phase': phase, 'captured_at': self.start + at}
                  for at, used, phase in ((60, 10, 'start'), (120, 15, 'end'))]
        path = self.root / 'message-costs-test.jsonl'
        path.write_text(''.join(json.dumps(e) + '\n' for e in events), encoding='utf-8')
        quotas.sync(self.root)
        self.ingest({**self.event(18, 180), 'source': 'accs'})
        samples = quotas.cycles(self.root, 'one')[0]['samples']
        self.assertEqual([s['source'] for s in samples], ['claude-code', 'claude-code', 'accs'])
        self.assertEqual(samples[1]['task_started_at'], self.start + 60)

    def test_countdown_and_deadline_correction_keep_one_period(self):
        self.ingest(self.event(10, 60), self.event(15, 120), self.event(20, 180, self.week + 30))
        periods = quotas.cycles(self.root, "one")
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]["used"], 20)
        self.assertEqual(len(periods[0]["revisions"]), 1)
        self.assertEqual(periods[0]["start"], self.start + 30)
        self.assertEqual(periods[0]["status"], "observed")

    def test_rollover_archive_is_order_independent_deduplicated_and_account_scoped(self):
        old = self.event(80, self.week - 60)
        new = self.event(10, self.week + 60, 2 * self.week)
        short = self.event(30, 3600, key="short", seconds=5 * 3600)
        self.ingest(new, old, short, old, self.event(99, 120, account="another"))
        periods = quotas.cycles(self.root, "one")
        weekly = [p for p in periods if p["window"] == "weekly"]
        self.assertEqual([p["used"] for p in weekly], [80, 10])
        self.assertEqual([p["status"] for p in weekly], ["reset_confirmed", "observed"])
        self.assertEqual(len(periods), 3)
        self.assertEqual(len(weekly[0]["samples"]), 1)
        self.assertEqual(quotas.cycles(self.root, "another")[0]["used"], 99)
        self.assertEqual(quotas.cycles(self.root, "one"), periods)

    def test_old_identity_joins_new_identity_and_unknown_resets_remain_archived(self):
        unknown = self.event(12, 120, key="short")
        unknown["windows"][0]["reset_at"] = None
        current = self.event(15, 180, account="canonical")
        current["legacy_accounts"] = ["one"]
        self.ingest(self.event(10, 60), unknown, current)
        period = quotas.cycles(self.root, "canonical")[0]
        self.assertEqual([s["used"] for s in period["samples"]], [10, 15])
        with closing(quotas.connect(self.root)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM quota_cycles WHERE end IS NULL").fetchone()[0], 1)
        self.assertEqual(quotas.cycles(self.root, None), [])

    def test_sync_recovers_jsonl_append_and_prepend_without_duplication(self):
        path = self.root / "account-usage-samples.jsonl"
        first = json.dumps(self.event(10, 60)).encode() + b"\n"
        second = json.dumps(self.event(20, 120)).encode() + b"\n"
        path.write_bytes(second + first[:30])
        quotas.sync(self.root)
        self.assertEqual(len(quotas.cycles(self.root, "one")[0]["samples"]), 1)
        path.write_bytes(first + second)
        quotas.sync(self.root)
        quotas.sync(self.root)
        self.assertEqual(len(quotas.cycles(self.root, "one")[0]["samples"]), 2)

    def test_provider_deadline_sets_graph_boundaries_and_short_resets_sum(self):
        self.ingest(self.event(41, 3600), self.event(50, 3600, key="short", seconds=18000),
                    self.event(20, 21600, end=36000, key="short", seconds=18000))
        cycles = quotas.cycles(self.root, "one")
        paid = {"start": datetime.fromtimestamp(self.start - 600).astimezone().isoformat(),
                "end": datetime.fromtimestamp(self.start + self.week * 4).astimezone().isoformat()}
        weeks = history.provider_weeks(cycles, paid)
        self.assertEqual(datetime.fromisoformat(weeks[0]["start"]).timestamp(), self.start)
        self.assertFalse(weeks[0]["estimated"])
        self.assertTrue(weeks[1]["estimated"])
        samples = [{**s, "account": "one", "key": p["window"], "label": p["label"],
                    "reset_at": p["end"], "duration_seconds": p["duration"]}
                   for p in cycles for s in p["samples"]]
        totals = history.weekly_usage(samples, paid, "one", boundaries=weeks)[0]["windows"]
        self.assertEqual({w["key"]: w["percent"] for w in totals}, {"weekly": 41, "short": 70})

    def test_stable_period_id_and_no_rows_for_unchanged_usage(self):
        self.ingest(self.event(38, 60))
        period_id = quotas.cycles(self.root, "one")[0]['id']
        self.ingest(*(self.event(38, n) for n in range(61, 120)))
        with closing(quotas.connect(self.root)) as db:
            row = db.execute('SELECT * FROM quota_samples').fetchone()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quota_samples').fetchone()[0], 1)
            self.assertEqual((row['period_id'], row['first_seen'], row['last_seen']),
                             (period_id, self.start + 60, self.start + 119))
        self.ingest(self.event(39, 120, self.week + 30))
        self.assertEqual(quotas.cycles(self.root, 'one')[0]['id'], period_id)
        self.ingest(self.event(39, self.week + 60, 2 * self.week))
        periods = quotas.cycles(self.root, 'one')
        self.assertEqual(periods[0]['id'], period_id)
        self.assertNotEqual(periods[1]['id'], period_id)

    def test_subscriptions_stored_once_and_rates_never_stored(self):
        event = self.event(10, 60)
        event['subscriptionPeriod'] = {'start': '2026-09-01T12:00:00+03:00', 'end': '2026-10-01T12:00:00+03:00'}
        event['rates'] = {'UNWANTED_API_RATE': 123}
        self.ingest(event, event)
        event['subscriptionPeriod'] = {'start': '2026-10-01T12:00:00+03:00', 'end': '2026-11-01T12:00:00+03:00'}
        self.ingest(event)
        self.assertEqual(len(quotas.subscription_periods(self.root, 'one')), 2)
        with closing(quotas.connect(self.root)) as db:
            self.assertNotIn('UNWANTED_API_RATE', '\n'.join(db.iterdump()))

    def test_v1_database_migration_is_atomic_and_keeps_periods(self):
        with closing(sqlite3.connect(self.root / 'quota-periods.sqlite3')) as db, db:
            db.execute('CREATE TABLE observations (id TEXT, provider TEXT, account TEXT, profile TEXT, window TEXT, label TEXT, observed REAL, reset REAL, duration REAL, used REAL, expired INTEGER)')
            for index in range(5):
                db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                           (str(index), 'test', 'one', 'profile', 'weekly', '7 дн', self.start + index, self.start + self.week, self.week, 38, 0))
        periods = quotas.cycles(self.root, 'one')
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]['used'], 38)
        with closing(quotas.connect(self.root)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM quota_samples').fetchone()[0], 1)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone())
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertEqual(quotas.cycles(self.root, 'one')[0]['id'], periods[0]['id'])

    def test_late_alias_registration_merges_same_week_without_double_counting(self):
        self.ingest(self.event(10, 60, account='old-profile'), self.event(20, 120, account='canonical'))
        original_id = quotas.cycles(self.root, 'canonical')[0]['id']
        event = self.event(20, 180, account='canonical')
        event['legacy_accounts'] = ['old-profile']
        self.ingest(event)
        periods = quotas.cycles(self.root, 'canonical')
        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0]['id'], original_id)
        self.assertEqual(periods[0]['used'], 20)
        self.assertEqual(periods[0]['samples'][0]['used'], 10)


if __name__ == "__main__":
    unittest.main()
