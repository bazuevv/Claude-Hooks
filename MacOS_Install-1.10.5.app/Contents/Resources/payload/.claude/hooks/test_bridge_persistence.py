import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codex_anthropic_bridge import CodexTextBackend, collect_message, DISPATCH_TOOL, _message_fingerprints
from test_codex_anthropic_bridge import PersistentSessionTests


class PersistenceTests(unittest.TestCase):
    class Client(PersistentSessionTests.FakeClient):
        def __init__(self, backend):
            super().__init__()
            self.backend = backend

        def request(self, method, params):
            if method == 'thread/resume':
                self.requests.append((method, params))
                return {'thread': {'id': params['threadId']}}
            result = super().request(method, params)
            if method == 'turn/start':
                tid = params['threadId']
                turn = result['turn']['id']
                self.backend._handle_notification({'method': 'thread/tokenUsage/updated', 'params': {
                    'threadId': tid, 'tokenUsage': {'last': {'inputTokens': 1234}, 'total': {}}}})
                for method, extra in [('item/agentMessage/delta', {'delta': 'Summary of history'}),
                                      ('turn/completed', {'turn': {'status': 'completed'}})]:
                    self.backend.router.dispatch({'method': method, 'params': {
                        'threadId': tid, 'turnId': turn, **extra}})
            return result

    def backend(self, path=None):
        backend = CodexTextBackend(timeout=1, usage_state_file=str(path) if path else None)
        backend.client = self.Client(backend)
        return backend

    def test_restart_resumes_codex_thread_without_history_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.json'
            first = self.backend(path)
            payload = PersistentSessionTests.payload([{'role': 'user', 'content': 'OLD CONTEXT'}])
            collect_message(first.begin(payload))
            self.assertFalse(first.client.requests[0][1]['ephemeral'])
            second = self.backend(path)
            payload['messages'].append({'role': 'user', 'content': 'NEW TASK'})
            collect_message(second.begin(payload))
            self.assertEqual(second.client.requests[0][0], 'thread/resume')
            self.assertNotIn('thread/start', [m for m, _ in second.client.requests])
            prompt = second.client.requests[-1][1]['input'][0]['text']
            self.assertIn('NEW TASK', prompt)
            self.assertNotIn('OLD CONTEXT', prompt)

    def test_legacy_history_recovery_needs_only_one_model_request(self):
        backend = self.backend()
        payload = PersistentSessionTests.payload([
            {'role': 'user', 'content': 'Historical data. ' * 20000},
            {'role': 'assistant', 'content': 'Earlier response'},
            {'role': 'user', 'content': 'NEW TASK'},
        ])
        collect_message(backend.begin(payload))
        starts = [p for m, p in backend.client.requests if m == 'thread/start']
        self.assertEqual(len(starts), 1)
        turns = [p for m, p in backend.client.requests if m == 'turn/start']
        self.assertEqual(len(turns), 1)
        self.assertLess(max(len(p['input'][0]['text']) for p in turns), 120000)
        self.assertIn('NEW TASK', turns[-1]['input'][0]['text'])
        self.assertIn('complete original history is saved', turns[-1]['input'][0]['text'])

    @staticmethod
    def bloated_payload():
        return PersistentSessionTests.payload([
            {'role': 'system', 'content': 'OLD FILE NOTIFICATION ' * 10000},
            {'role': 'system', 'content': '## Re-entering Plan Mode\nOLD mode'},
            {'role': 'user', 'content': 'Implement the accepted task'},
            {'role': 'system', 'content': '## Exited Plan Mode\nCURRENT mode'},
        ])

    def test_recovery_bounds_instructions_before_creating_the_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(Path(tmp) / 'usage.json')
            payload = self.bloated_payload()
            collect_message(backend.begin(payload))
            start = next(p for m, p in backend.client.requests if m == 'thread/start')
            self.assertNotIn('OLD FILE NOTIFICATION', start['developerInstructions'])
            self.assertNotIn('OLD mode', start['developerInstructions'])
            self.assertIn('CURRENT mode', start['developerInstructions'])
            self.assertLess(len(start['developerInstructions']), 20000)
            self.assertEqual(backend._sessions['claude-1'].seen_messages, _message_fingerprints(payload))
            archives = list((Path(tmp) / 'recovered-history').glob('*.json'))
            self.assertEqual(len(archives), 1)
            self.assertEqual(json.loads(archives[0].read_text(encoding='utf-8'))['messages'], payload['messages'])

    def test_legacy_bloated_checkpoint_is_migrated_once_at_next_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'usage.json'
            first = self.backend(path)
            collect_message(first.begin(PersistentSessionTests.payload([{'role': 'user', 'content': 'old'}])))
            checkpoint = Path(first._checkpoint_path('claude-1'))
            saved = json.loads(checkpoint.read_text(encoding='utf-8'))
            saved.pop('recovery_version')
            saved['thread_id'] = 'legacy-bloated-thread'
            checkpoint.write_text(json.dumps(saved), encoding='utf-8')
            second = self.backend(path)
            payload = self.bloated_payload()
            collect_message(second.begin(payload))
            self.assertNotIn('thread/resume', [m for m, _ in second.client.requests])
            current = json.loads(checkpoint.read_text(encoding='utf-8'))
            self.assertNotEqual(current['thread_id'], saved['thread_id'])
            third = self.backend(path)
            payload['messages'].append({'role': 'user', 'content': 'next'})
            collect_message(third.begin(payload))
            self.assertEqual(third.client.requests[0][0], 'thread/resume')
            self.assertNotIn('thread/start', [m for m, _ in third.client.requests])

    def test_failed_migration_preserves_old_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = self.backend(Path(tmp) / 'usage.json')
            checkpoint = Path(backend._checkpoint_path('claude-1'))
            checkpoint.parent.mkdir()
            saved = {'thread_id': 'legacy-bloated-thread'}
            checkpoint.write_text(json.dumps(saved), encoding='utf-8')
            with mock.patch.object(backend, '_start_turn', side_effect=RuntimeError('start failed')):
                with self.assertRaisesRegex(RuntimeError, 'start failed'):
                    backend.begin(self.bloated_payload())
            self.assertEqual(json.loads(checkpoint.read_text()), saved)
            self.assertFalse(backend._sessions)

    def test_dispatcher_uses_current_tool_catalog(self):
        backend = self.backend()
        payload = PersistentSessionTests.payload([{'role': 'user', 'content': 'task'}])
        collect_message(backend.begin(payload))
        session = backend._sessions['claude-1']
        session.tool_names['ListAgents'] = 'ListAgents'
        result = []
        worker = threading.Thread(target=lambda: result.append(backend._handle_server_request({
            'method': 'item/tool/call', 'params': {'threadId': session.thread_id,
            'tool': DISPATCH_TOOL, 'callId': 'new-tool',
            'arguments': {'name': 'ListAgents', 'arguments': {}}}})))
        worker.start()
        call = session.tool_calls.get(timeout=1)
        self.assertEqual(call.name, 'ListAgents')
        call.resolve({'success': True, 'contentItems': []})
        worker.join(timeout=1)
        self.assertTrue(result[0]['success'])


if __name__ == '__main__':
    unittest.main()
