import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from codex_anthropic_bridge import CodexTextBackend
from test_codex_anthropic_bridge import PersistentSessionTests


class AuditTests(unittest.TestCase):
    def test_ui_uses_pre_compaction_input_and_records_each_item_in_one_turn(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        backend.begin(PersistentSessionTests.payload([{'role': 'user', 'content': 'hello'}]))
        session = backend._sessions['claude-1']
        session.prior_context = 234591  # Stale start-of-turn count from the reported bug.

        def usage(value):
            backend._handle_notification({'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': 'thread-1', 'tokenUsage': {'last': {
                    'inputTokens': value, 'outputTokens': 385 if value else 0,
                }, 'modelContextWindow': 258400}}})

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            'codex_anthropic_bridge.COMPACTION_LOG_FILE', str(Path(tmp)/'audit.jsonl')
        ), mock.patch('codex_anthropic_bridge.CONTEXT_EVENTS_FILE', str(Path(tmp)/'ui.jsonl')):
            for item_id, before, after in [('c1', 237218, 235375), ('c2', 243603, 236117)]:
                usage(before)
                params = {'threadId': 'thread-1', 'turnId': 'turn-1',
                          'item': {'id': item_id, 'type': 'contextCompaction'}}
                backend._handle_notification({'method': 'item/started', 'params': params})
                usage(0)  # Synthetic compaction telemetry must not erase the snapshot.
                backend._handle_notification({'method': 'item/completed', 'params': params})
                backend._handle_notification({'method': 'thread/compacted', 'params': {
                    'threadId': 'thread-1', 'turnId': 'turn-1'}})
                usage(0)  # Nor can it become a measurement of the compacted context.
                self.assertIsNotNone(session.pending_compaction)
                usage(after)
            rows = [json.loads(line) for line in (Path(tmp)/'ui.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['event_id'], rows[1]['event_id'])
        self.assertEqual(rows[2]['event_id'], rows[3]['event_id'])
        self.assertNotEqual(rows[0]['event_id'], rows[2]['event_id'])
        self.assertEqual([(r['context_before'], r['context_after']) for r in rows if 'context_after' in r],
                         [(237218, 235375), (243603, 236117)])

    def test_start_snapshot_survives_new_turn_and_completion(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        backend.begin(PersistentSessionTests.payload([{'role': 'user', 'content': 'hello'}]))
        session = backend._sessions['claude-1']
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            'codex_anthropic_bridge.COMPACTION_LOG_FILE', str(Path(tmp)/'audit.jsonl')
        ), mock.patch('codex_anthropic_bridge.CONTEXT_EVENTS_FILE', str(Path(tmp)/'ui.jsonl')):
            backend._handle_notification({'method': 'thread/tokenUsage/updated', 'params': {
                'threadId': 'thread-1', 'tokenUsage': {'last': {
                    'inputTokens': 220000, 'cachedInputTokens': 210000, 'outputTokens': 2000,
                }, 'modelContextWindow': 258400}}})
            session.last_usage.clear()  # turn/start clears response usage
            params = {'threadId': 'thread-1', 'turnId': 'turn-1',
                      'item': {'id': 'c1', 'type': 'contextCompaction'}}
            for _ in range(2):
                backend._handle_notification({'method': 'item/started', 'params': params})
            session.last_usage.update(input_tokens=50000)
            backend._handle_notification({'method': 'item/completed', 'params': params})
            backend._handle_notification({'method': 'thread/compacted', 'params': {
                'threadId': 'thread-1', 'turnId': 'turn-1'}})
            rows = [json.loads(line) for line in (Path(tmp)/'audit.jsonl').read_text().splitlines()]
        self.assertEqual([r['phase'] for r in rows], ['started', 'completed'])
        self.assertEqual(rows[0]['context_tokens_at_start'], 222000)
        self.assertEqual(rows[1]['context_tokens_at_start'], 222000)
        self.assertEqual(rows[0]['context_window'], 258400)
        self.assertEqual(rows[0]['started_at'], rows[1]['started_at'])

    def test_two_compactions_in_one_turn_are_logged_separately(self):
        backend = CodexTextBackend()
        backend.client = PersistentSessionTests.FakeClient()
        backend.begin(PersistentSessionTests.payload([{'role': 'user', 'content': 'hello'}]))
        session = backend._sessions['claude-1']
        with mock.patch.object(backend, '_append_context_event') as write:
            for item_id in ['c1', 'c2']:
                backend._audit_compaction(session, {'turnId': 'turn-1', 'item': {'id': item_id}}, 'item/started')
        self.assertEqual(write.call_count, 2)
        self.assertNotEqual(write.call_args_list[0].args[0]['event_id'], write.call_args_list[1].args[0]['event_id'])

    def test_missing_start_does_not_invent_measurement(self):
        backend = CodexTextBackend()
        backend.client = PersistentSessionTests.FakeClient()
        backend.begin(PersistentSessionTests.payload([{'role': 'user', 'content': 'hello'}]))
        with mock.patch.object(backend, '_append_context_event') as write:
            backend._audit_compaction(backend._sessions['claude-1'], {'turnId': 'turn-1'}, 'thread/compacted')
        event = write.call_args.args[0]
        self.assertIsNone(event['started_at'])
        self.assertIsNone(event['context_tokens_at_start'])


if __name__ == '__main__':
    unittest.main()
