import http.client
import json
import queue
import threading
import unittest
from unittest import mock

from codex_anthropic_bridge import (
    BridgeError, BridgeHttpServer, CodexTextBackend, DynamicToolCall,
    TextTurn, _anthropic_usage, collect_message, is_compaction_request,
    _has_new_user_input, _message_fingerprints,
    STREAM_PING, USAGE_READY, stream_events,
)
from test_codex_anthropic_bridge import PersistentSessionTests
from codex_app_server import CodexRpcError


SUMMARY = (
    'CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n\n'
    'Your task is to create a detailed summary of the conversation so far'
)


class CompactionTests(unittest.TestCase):
    def test_compacted_history_replaces_pending_thread_before_tool_resume(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        payload = PersistentSessionTests.payload
        first = backend.begin(payload([{'role': 'user', 'content': 'OLD HISTORY'}]))
        old = backend._sessions['claude-1']
        old.last_usage['input_tokens'] = 226000
        call = DynamicToolCall('call-1', 'Read', {}, queue.Queue())
        old.pending_tools['call-1'] = call
        old.tool_calls.put(call)
        collect_message(first)
        summary = ('This session is being continued from a previous conversation '
                   'that ran out of context.\nSummary: retain current task')
        compacted = payload([
            {'role': 'user', 'content': summary},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-1',
                                             'name': 'Read', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-1',
                                         'content': 'current file'}]},
        ])
        second = backend.begin(compacted)
        new = backend._sessions['claude-1']
        self.assertEqual(new.thread_id, 'thread-2')
        starts = [p for m, p in backend.client.requests if m == 'turn/start']
        self.assertIn('retain current task', starts[-1]['input'][0]['text'])
        self.assertNotIn('OLD HISTORY', starts[-1]['input'][0]['text'])
        new.last_usage['input_tokens'] = 1200
        new.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-2', 'turn': {'status': 'completed'},
        }})
        self.assertEqual(collect_message(second)['usage']['input_tokens'], 1200)
        compacted['messages'].append({'role': 'user', 'content': 'Continue'})
        third = backend.begin(compacted)
        self.assertIs(backend._sessions['claude-1'], new)
        new.last_usage['input_tokens'] = 1300
        new.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-3', 'turn': {'status': 'completed'},
        }})
        collect_message(third)

    def test_tool_free_security_check_keeps_working_thread(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        payload = PersistentSessionTests.payload
        first = backend.begin(payload([{'role': 'user', 'content': 'Read a file'}]))
        original = backend._sessions['claude-1']
        call = DynamicToolCall('call-1', 'Read', {}, queue.Queue())
        original.pending_tools['call-1'] = call
        original.tool_calls.put(call)
        collect_message(first)
        check = payload([{'role': 'user', 'content': 'Evaluate this action'}])
        check['tools'] = []
        check['system'] = 'You are a security monitor for autonomous AI coding agents.'
        turn = backend.begin(check)
        self.assertIs(backend._sessions['claude-1'], original)
        self.assertIn('call-1', original.pending_tools)
        isolated = backend._tool_queues['thread-2']
        self.assertFalse(isolated.publish_usage)
        isolated.last_usage['input_tokens'] = 100
        isolated.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-2', 'turn': {'status': 'completed'},
        }})
        collect_message(turn)

    def test_added_tools_use_dispatcher_without_replacing_thread(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        payload = PersistentSessionTests.payload
        initial = payload([{'role': 'user', 'content': 'Read a file'}])
        first = backend.begin(initial)
        old = backend._sessions['claude-1']
        old.last_usage['input_tokens'] = 100
        old.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-1', 'turn': {'status': 'completed'},
        }})
        collect_message(first)
        initial['tools'].append({'name': 'Edit', 'input_schema': {'type': 'object'}})
        initial['messages'].append({'role': 'user', 'content': 'Edit the file'})
        second = backend.begin(initial)
        starts = [p for method, p in backend.client.requests if method == 'thread/start']
        self.assertEqual(len(starts), 1)
        self.assertIn('claude_bridge_dispatch', {t['name'] for t in starts[0]['dynamicTools']})
        self.assertIn('Edit', backend.client.requests[-1][1]['input'][0]['text'])
        new = backend._sessions['claude-1']
        self.assertEqual(new.thread_id, 'thread-1')
        new.last_usage['input_tokens'] = 100
        new.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-2', 'turn': {'status': 'completed'},
        }})
        collect_message(second)

    def test_keepalive_during_usage_priming_and_response(self):
        usage = {}
        def chunks():
            yield STREAM_PING
            usage.update({'input_tokens': 100, 'cached_input_tokens': 80})
            yield USAGE_READY
            yield STREAM_PING
            yield 'summary'
        events = list(stream_events(TextTurn('msg-1', 'test', chunks(), usage)))
        self.assertEqual(events[0], ('ping', {'type': 'ping'}))
        self.assertEqual(events[1][0], 'message_start')
        self.assertEqual(events[1][1]['message']['usage']['input_tokens'], 20)
        self.assertEqual(events[2][0], 'ping')
        self.assertEqual(events[-1][0], 'message_stop')

    def test_waiting_backend_produces_keepalive(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        turn = backend.begin(PersistentSessionTests.payload([
            {'role': 'user', 'content': SUMMARY},
        ]))
        with mock.patch('codex_anthropic_bridge.STREAM_PING_INTERVAL', 0.01):
            self.assertIs(next(turn.chunks), STREAM_PING)
        turn.chunks.close()
        self.assertFalse(backend._tool_queues['thread-1'].response_lock.locked())

    def test_merged_prompt_after_tool_result_is_new_input(self):
        old = {'messages': [{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'old-call', 'content': 'done'},
            {'type': 'text', 'text': '[Image: original 2560x1080]'},
        ]}]}
        merged = json.loads(json.dumps(old))
        blocks = merged['messages'][0]['content']
        blocks[1]['text'] += '\n'
        blocks.append({'type': 'text', 'text': '[Request interrupted by user]'})
        self.assertFalse(_has_new_user_input(merged, _message_fingerprints(old)))
        blocks.append({'type': 'text', 'text': 'Change the health bar colors'})
        self.assertTrue(_has_new_user_input(merged, _message_fingerprints(old)))
        self.assertFalse(_has_new_user_input(merged, _message_fingerprints(merged)))

    def test_new_user_after_reload_recovers_abandoned_tool_wait(self):
        self._check_reload_recovery()

    def test_reload_recovers_when_old_turn_already_finished(self):
        self._check_reload_recovery('no active turn to interrupt')

    def test_reload_preserves_other_interrupt_errors(self):
        with self.assertRaisesRegex(CodexRpcError, 'thread not found'):
            self._check_reload_recovery('thread not found')

    def _check_reload_recovery(self, interrupt_error=None):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        original_request = backend.client.request

        def request(method, params):
            result = original_request(method, params)
            if method == 'turn/interrupt' and interrupt_error:
                raise CodexRpcError(method, {'code': -32600, 'message': interrupt_error})
            return result

        backend.client.request = request
        payload = PersistentSessionTests.payload
        initial = [{'role': 'user', 'content': 'Read a file'}]
        first = backend.begin(payload(initial))
        original = backend._sessions['claude-1']
        call = DynamicToolCall('call-1', 'Read', {}, queue.Queue())
        original.pending_tools['call-1'] = call
        original.tool_calls.put(call)
        collect_message(first)
        with self.assertRaisesRegex(BridgeError, 'waiting'):
            backend.begin(payload(initial))
        self.assertIn('call-1', original.pending_tools)
        resumed = backend.begin(payload(initial + [
            {'role': 'user', 'content': 'Continue'},
            {'role': 'system', 'content': 'Environment update'},
        ]))
        replacement = backend._sessions['claude-1']
        self.assertIs(replacement, original)
        self.assertIn('thread-1', backend._tool_queues)
        self.assertFalse(call._response.get_nowait()['success'])
        self.assertIn(('turn/interrupt', {
            'threadId': 'thread-1', 'turnId': 'turn-1',
        }), backend.client.requests)
        replacement.last_usage['input_tokens'] = 100
        replacement.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-2', 'turn': {'status': 'completed'},
        }})
        collect_message(resumed)

    def test_cache_is_counted_once(self):
        usage = _anthropic_usage({'input_tokens': 91923, 'cached_input_tokens': 90112})
        self.assertEqual(usage['input_tokens'], 1811)
        self.assertEqual(sum(usage[k] for k in (
            'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens',
        )), 91923)
        self.assertEqual(_anthropic_usage({
            'input_tokens': 10, 'cached_input_tokens': 20,
        })['input_tokens'], 0)

    def test_summary_isolated_while_host_tool_is_pending(self):
        backend = CodexTextBackend(timeout=1)
        backend.client = PersistentSessionTests.FakeClient()
        payload = PersistentSessionTests.payload
        first = backend.begin(payload([{'role': 'user', 'content': 'Read a file'}]))
        original = backend._sessions['claude-1']
        call = DynamicToolCall('call-1', 'Read', {}, queue.Queue())
        original.pending_tools['call-1'] = call
        original.tool_calls.put(call)
        collect_message(first)
        summary_payload = payload([{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'contents'},
            {'type': 'text', 'text': SUMMARY},
        ]}])
        summary = backend.begin(summary_payload)
        isolated = backend._tool_queues['thread-2']
        self.assertIs(backend._sessions['claude-1'], original)
        self.assertIn('call-1', original.pending_tools)
        self.assertTrue(call._response.empty())
        self.assertFalse(isolated.publish_usage)
        starts = [p for m, p in backend.client.requests if m == 'thread/start']
        self.assertNotIn('dynamicTools', starts[-1])
        isolated.last_usage['input_tokens'] = 100
        isolated.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-2', 'turn': {'status': 'completed'},
        }})
        collect_message(summary)
        resumed = backend.begin(payload([{'role': 'user', 'content': [{
            'type': 'tool_result', 'tool_use_id': 'call-1', 'content': 'contents',
        }]}]))
        self.assertFalse(call._response.empty())
        original.last_usage['input_tokens'] = 100
        original.events.put({'method': 'turn/completed', 'params': {
            'turnId': 'turn-1', 'turn': {'status': 'completed'},
        }})
        collect_message(resumed)

    def test_quoted_summary_does_not_isolate_normal_turn(self):
        self.assertFalse(is_compaction_request({'messages': [
            {'role': 'user', 'content': SUMMARY},
            {'role': 'assistant', 'content': 'summary'},
            {'role': 'user', 'content': 'Continue'},
        ]}))


class HttpErrorTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.Mock()
        self.server = BridgeHttpServer(('127.0.0.1', 0), self.backend)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()

    def test_rejected_post_closes_connection_with_unread_body(self):
        self.connection.request('POST', '/v1/messages/count_tokens', '{}')
        response = self.connection.getresponse()
        self.assertEqual(response.status, 404)
        self.assertEqual(response.getheader('Connection'), 'close')
        response.read()
        self.connection.request('GET', '/health')
        self.assertTrue(json.loads(self.connection.getresponse().read())['ok'])

    def test_stream_failure_is_sse_error_not_second_http_response(self):
        def chunks():
            yield 'partial'
            raise BridgeError('summary failed')
        self.backend.begin.return_value = TextTurn('msg-1', 'test', chunks())
        with mock.patch('codex_anthropic_bridge.capture_claude_payload'):
            self.connection.request('POST', '/v1/messages', json.dumps({'stream': True}))
            response = self.connection.getresponse()
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn('event: error\n', body)
        self.assertIn('summary failed', body)
        self.assertNotIn('HTTP/1.1', body)


if __name__ == '__main__':
    unittest.main()
