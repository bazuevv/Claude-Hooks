"""Automatic discovery, recovery and bridge startup without switching accounts."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest import mock

import openai_bootstrap as bootstrap
import account_switcher as accounts
import codex_bridge_manager as bridge

CATALOG = [{"id": model} for model in accounts.OPENAI_MODEL_PRIORITY]
SNAPSHOT = {"account": {"type": "chatgpt"}, "models": CATALOG}


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="автонастройка OpenAI ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "settings_openai.json"
        self.active = self.root / "settings.json"
        self.active.write_text('{"env":{"ANTHROPIC_BASE_URL":"https://other.invalid"},"model":"other"}')
        self.original = self.active.read_bytes()
        self.shared = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "shared"}]}]},
                       "permissions": {"allow": ["Read"], "deny": ["Bash(blocked)"]}}
        (self.root / "settings-hooks.json").write_text(json.dumps(self.shared))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.multiple(accounts, CLAUDE_DIR=str(self.root), SETTINGS_FILE=str(self.active)))
        stack.enter_context(mock.patch.multiple(bootstrap, _last_check=None, _last_result=(False, "not checked")))
        self.client = stack.enter_context(mock.patch.object(bootstrap, "CodexAppServerClient"))
        self.client.return_value.__enter__.return_value.snapshot.return_value = {
            "account": {"account": SNAPSHOT["account"]}, "models": CATALOG}
        self.ensure = stack.enter_context(mock.patch.object(bridge, "ensure", return_value=(True, "ready")))
        self.log = stack.enter_context(mock.patch.object(bootstrap.hook_log, "log"))

    def test_first_check_creates_profile_with_shared_permissions_without_switching(self):
        self.assertTrue(bootstrap.ensure_ready()[0])
        saved = json.loads(self.profile.read_text())
        self.assertEqual(list(saved)[:2], ["hooks", "permissions"])
        self.assertEqual(saved["permissions"], self.shared["permissions"])
        self.assertEqual(saved["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"], accounts.OPENAI_MODEL_PRIORITY[0])
        self.assertEqual(self.active.read_bytes(), self.original)
        self.assertFalse((self.root / ".active-account").exists())
        self.ensure.assert_called_once()

    def test_existing_profile_is_not_rewritten_and_bridge_is_checked(self):
        original = '{"model":"custom","env":{"CUSTOM":"keep"}}\n'
        self.profile.write_text(original)
        for _ in range(2):
            self.assertTrue(bootstrap.ensure_ready(force=True)[0])
        self.assertEqual(self.profile.read_text(), original)
        self.client.assert_not_called()
        self.assertEqual(self.ensure.call_count, 2)

    def test_deleted_active_profile_recovers_custom_parameters(self):
        active = {"env": {"ANTHROPIC_BASE_URL": f"http://{bridge.BRIDGE_HOST}:{bridge.BRIDGE_PORT}",
                          "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "258400"}, "model": "my-model", "effortLevel": "high"}
        self.active.write_text(json.dumps(active))
        before = self.active.read_bytes()
        self.assertTrue(bootstrap.create_profile(SNAPSHOT))
        saved = json.loads(self.profile.read_text())
        self.assertEqual(saved["env"], active["env"])
        self.assertEqual(saved["model"], "my-model")
        self.assertEqual(self.active.read_bytes(), before)

    def test_no_login_then_later_login_retries_and_creates_profile(self):
        snapshot = self.client.return_value.__enter__.return_value.snapshot
        snapshot.return_value = {"account": {"account": None}, "models": CATALOG}
        self.assertFalse(bootstrap.ensure_ready()[0])
        self.assertFalse(self.profile.exists())
        self.ensure.assert_not_called()
        self.assertFalse(bootstrap.ensure_ready()[0])
        self.assertEqual(snapshot.call_count, 1, "No retry storm while logged out")
        snapshot.return_value = {"account": {"account": SNAPSHOT["account"]}, "models": CATALOG}
        self.assertTrue(bootstrap.ensure_ready(force=True)[0])
        self.assertTrue(self.profile.exists())

    def test_bridge_failure_does_not_publish_profile_and_is_retried(self):
        self.ensure.return_value = False, "not running"
        self.assertFalse(bootstrap.ensure_ready()[0])
        self.assertFalse(self.profile.exists())
        self.ensure.return_value = True, "started"
        self.assertTrue(bootstrap.ensure_ready(force=True)[0])
        self.assertTrue(self.profile.exists())

    def test_atomic_creation_preserves_concurrently_created_profile(self):
        link = os.link
        def another_writer(source, destination):
            self.profile.write_text('{"model":"created elsewhere"}')
            return link(source, destination)
        with mock.patch.object(bootstrap.os, "link", side_effect=another_writer):
            self.assertFalse(bootstrap.create_profile(SNAPSHOT))
        self.assertEqual(json.loads(self.profile.read_text()), {"model": "created elsewhere"})
        self.assertEqual(list(self.root.glob(".openai-*")), [])

    def test_invalid_shared_settings_leave_active_file_and_profile_unchanged(self):
        (self.root / "settings-hooks.json").write_text("broken")
        self.assertFalse(bootstrap.ensure_ready()[0])
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.active.read_bytes(), self.original)

    def test_monitor_recreates_deleted_profile_on_next_pass(self):
        stop = mock.Mock()
        stop.is_set.return_value = False
        calls = []
        def tick(interval):
            self.assertEqual(interval, 30)
            calls.append(self.profile.exists())
            if len(calls) == 1:
                self.profile.unlink()
                return False
            return True
        stop.wait.side_effect = tick
        bootstrap.run_monitor(stop)
        self.assertEqual(calls, [True, True])

    def test_simultaneous_requests_do_not_start_multiple_checks(self):
        with bootstrap._LOCK:
            bootstrap.ensure_ready(force=True)
        self.client.assert_not_called()
        self.ensure.assert_not_called()


class BridgeStartupPlatformTests(unittest.TestCase):
    def test_absent_bridge_is_spawned_on_all_platforms(self):
        for name in ("posix", "nt"):
            with self.subTest(os_name=name), \
                    mock.patch.object(bridge, "os", types.SimpleNamespace(name=name)), \
                    mock.patch.object(bridge.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                    mock.patch.object(bridge.subprocess, "Popen") as spawn, \
                    mock.patch.object(bridge, "health", side_effect=[None, {"service": "claude-openai-bridge"}]):
                self.assertTrue(bridge.ensure()[0])
                spawn.assert_called_once()
                self.assertEqual(spawn.call_args.kwargs["creationflags"], 0x08000000 if name == "nt" else 0)
                self.assertIn(bridge.BRIDGE_SCRIPT, spawn.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
