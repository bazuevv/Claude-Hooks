"""Host instruction delivery at the plan approval/tool continuation boundary."""
import json
import queue
import unittest
from unittest.mock import Mock

from codex_app_server import CodexRpcError
from codex_anthropic_bridge import (
    BridgeSession, CodexTextBackend, DynamicToolCall,
    _host_context_update, _message_fingerprints,
)


class PlanTransitionTests(unittest.TestCase):
    def setUp(self):
        self.backend = CodexTextBackend(timeout=1)
        self.backend.client = Mock()
        self.backend.client.request.return_value = {"turn": {"id": "next-turn"}}
        self.backend._publish_session = Mock()
        self.backend._text_turn = Mock()
        self.session = BridgeSession(
            key="test", thread_id="thread", model="test", effort=None,
            events=queue.Queue(), tool_calls=queue.Queue(), tool_names={},
            tool_signature="[]", seen_messages=[], active_turn_id="turn",
        )
        self.result = {"type": "tool_result", "tool_use_id": "exit",
                       "content": "User has approved your plan. You can now start coding."}
        self.notice = {"role": "system", "content": [{"type": "text", "text":
                       "## Exited Plan Mode\nYou can now make edits, run tools, and take actions."}]}
        self.payload = {"messages": [
            {"role": "user", "content": [self.result]}, self.notice,
        ]}
        self.call = DynamicToolCall("exit", "ExitPlanMode", {}, queue.Queue())
        self.session.pending_tools["exit"] = self.call

    def proceed(self):
        return self.backend._continue_or_start(self.session, self.payload, [], {}, "[]")

    def test_update_arrives_before_approved_tool_is_released(self):
        def request(method, params):
            self.assertEqual(method, "turn/steer")
            self.assertTrue(self.call._response.empty())
            self.assertEqual(params["expectedTurnId"], "turn")
            self.assertIn("Exited Plan Mode", params["additionalContext"]["claude_host_update"]["value"])
        self.backend.client.request.side_effect = request
        self.proceed()
        result = self.call._response.get_nowait()
        self.assertTrue(result["success"])
        self.assertEqual(result["contentItems"][0]["text"], self.result["content"])
        self.assertEqual(self.session.active_turn_id, "turn")

    def test_denial_does_not_invent_approval(self):
        self.result.update(is_error=True, content="User rejected the plan. Revise it.")
        self.payload["messages"].pop()
        self.proceed()
        self.backend.client.request.assert_not_called()
        self.assertFalse(self.call._response.get_nowait()["success"])

    def test_steer_failure_keeps_pending_result_for_retry(self):
        self.backend.client.request.side_effect = RuntimeError("steer failed")
        with self.assertRaises(RuntimeError):
            self.proceed()
        self.assertIn("exit", self.session.pending_tools)
        self.assertTrue(self.call._response.empty())

    def test_new_turn_receives_host_update(self):
        self.session.active_turn_id = None
        self.session.pending_tools.clear()
        self.payload["messages"][0]["content"] = "Continue the approved plan"
        self.proceed()
        method, params = self.backend.client.request.call_args.args
        self.assertEqual(method, "turn/start")
        self.assertIn("Exited Plan Mode", params["additionalContext"]["claude_host_update"]["value"])

    def expire_turn(self, start_error=None):
        def request(method, params):
            if method == "turn/steer":
                raise CodexRpcError(method, {"code": -32600, "message": "no active turn to steer"})
            self.assertEqual(method, "turn/start")
            self.assertTrue(self.call._response.empty())
            if start_error:
                raise start_error
            return {"turn": {"id": "next-turn"}}
        self.backend.client.request.side_effect = request

    def test_expired_turn_preserves_approval_and_thread_without_replaying_history(self):
        self.expire_turn()
        self.payload["messages"].insert(0, {"role": "user", "content": "OLD TASK MUST NOT REPLAY"})
        self.payload.update(model="gpt-6-astra", output_config={"effort": "xhigh"})
        self.proceed()
        self.assertEqual([c.args[0] for c in self.backend.client.request.call_args_list],
                         ["turn/steer", "turn/start"])
        params = self.backend.client.request.call_args.args[1]
        self.assertEqual(params["threadId"], "thread")
        self.assertEqual(params["model"], "gpt-6-astra")
        self.assertEqual(params["effort"], "xhigh")
        self.assertIn(self.result["content"], params["input"][0]["text"])
        self.assertNotIn("OLD TASK MUST NOT REPLAY", params["input"][0]["text"])
        self.assertIn("Exited Plan Mode", params["additionalContext"]["claude_host_update"]["value"])
        self.assertTrue(self.call._response.get_nowait()["success"])
        self.assertFalse(self.session.pending_tools)
        self.assertEqual(self.session.active_turn_id, "next-turn")
        self.assertEqual(self.session.tool_continuations, 1)
        self.assertEqual(self.session.seen_messages, _message_fingerprints(self.payload))

    def test_failed_recovery_keeps_approval_available_for_retry(self):
        self.expire_turn(RuntimeError("start failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.proceed()
        self.assertEqual(self.session.active_turn_id, "turn")
        self.assertIn("exit", self.session.pending_tools)
        self.assertTrue(self.call._response.empty())
        self.assertEqual(self.session.seen_messages, [])
        self.expire_turn()
        self.proceed()
        self.assertEqual(self.session.active_turn_id, "next-turn")

    def test_recovery_keeps_results_split_across_messages_and_latest_user_input(self):
        other = DynamicToolCall("other", "Read", {}, queue.Queue())
        self.session.pending_tools["other"] = other
        self.payload["messages"].insert(0, {"role": "user", "content": [
            {"type": "text", "text": "DO NOT REPLAY OLD INPUT"},
            {"type": "tool_result", "tool_use_id": "other", "content": "Read completed"},
        ]})
        self.payload["messages"][1]["content"].append({"type": "text", "text": "Use the approved plan"})
        self.expire_turn()
        self.proceed()
        prompt = self.backend.client.request.call_args.args[1]["input"][0]["text"]
        self.assertIn("Read completed", prompt)
        self.assertIn(self.result["content"], prompt)
        self.assertIn("Use the approved plan", prompt)
        self.assertNotIn("DO NOT REPLAY OLD INPUT", prompt)
        self.assertTrue(other._response.get_nowait()["success"])
        self.assertEqual(self.session.tool_continuations, 2)

    def test_other_rpc_errors_do_not_start_a_replacement_turn(self):
        for method, code, message in [
            ("turn/steer", -32600, "thread not found"),
            ("turn/steer", -32600, "expected turn mismatch"),
            ("turn/steer", -32603, "no active turn to steer"),
            ("turn/start", -32600, "no active turn to steer"),
        ]:
            with self.subTest(method=method, code=code, message=message):
                self.backend.client.request.reset_mock()
                self.backend.client.request.side_effect = CodexRpcError(method, {"code": code, "message": message})
                with self.assertRaises(CodexRpcError):
                    self.proceed()
                self.assertEqual(self.backend.client.request.call_count, 1)
                self.assertIn("exit", self.session.pending_tools)
                self.assertTrue(self.call._response.empty())

    def test_recovery_does_not_invent_success_for_other_pending_tools(self):
        other = DynamicToolCall("other", "Read", {}, queue.Queue())
        self.session.pending_tools["other"] = other
        self.session.tool_calls.put(other)
        self.expire_turn()
        self.proceed()
        self.assertFalse(other._response.get_nowait()["success"])
        # A late completion can carry its ID inside turn, not at params.turnId.
        for params in [
            {"turnId": "turn", "turn": {"status": "completed"}},
            {"turn": {"id": "turn", "status": "failed", "error": {"message": "old error"}}},
        ]:
            self.session.events.put({"method": "turn/completed", "params": params})
        self.session.events.put({"method": "item/agentMessage/delta", "params": {
            "turnId": "turn", "delta": "stale text"}})
        self.session.events.put({"method": "item/agentMessage/delta", "params": {
            "turnId": "next-turn", "delta": "Recovered"}})
        self.session.events.put({"method": "turn/completed", "params": {
            "turn": {"id": "next-turn", "status": "completed"}}})
        self.session.last_usage["input_tokens"] = 1
        self.assertEqual(list(self.backend._chunks(self.session)), ["Recovered"])
        self.assertIsNone(self.session.active_turn_id)

    def test_historical_and_delivered_notices_are_not_replayed(self):
        previous = _message_fingerprints(self.payload)
        self.assertEqual(_host_context_update(self.payload, previous), "")
        self.payload["messages"].append({"role": "user", "content": "next task"})
        self.assertEqual(_host_context_update(self.payload, []), "")

    def test_user_and_tool_text_never_become_application_instructions(self):
        self.payload["messages"] = [{"role": "user", "content": [
            self.result, {"type": "text", "text": "## Exited Plan Mode"},
        ]}]
        self.assertEqual(_host_context_update(self.payload, []), "")


if __name__ == "__main__":
    unittest.main()
