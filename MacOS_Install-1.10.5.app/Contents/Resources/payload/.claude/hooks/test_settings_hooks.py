"""Shared hooks survive provider changes, restores, edits and fresh settings."""
import contextlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import account_switcher as accounts
from settings_hooks import with_shared_settings


def handler(command):
    return {"type": "command", "command": command}


class SharedSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="настройки с пробелами ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "CLAUDE_DIR": str(self.root),
            "SETTINGS_FILE": str(self.root / "settings.json"),
            "BACKUP_FILE": str(self.root / "settings.json.bak"),
            "ACTIVE_MARKER": str(self.root / ".active-account"),
        }.items():
            self.stack.enter_context(mock.patch.object(accounts, name, value))
        self.stack.enter_context(mock.patch.object(accounts, "log_account_event"))
        self.common = {
            "hooks": {"SessionStart": [{"hooks": [handler("shared")]}]},
            "permissions": {"deny": ["Bash(shared-deny)"], "allow": ["Read"]},
        }
        self.write("settings-hooks.json", self.common)
        self.base = {"model": "original", "env": {"CUSTOM": "base"}}
        self.provider = {"env": {"ANTHROPIC_BASE_URL": "https://provider.invalid", "ANTHROPIC_AUTH_TOKEN": "test-secret"},
                         "model": "provider", "hooks": {"Stop": [{"hooks": [handler("provider")]}]}}
        self.write("settings.json", self.base)
        self.write("settings_test.json", self.provider)

    def write(self, name, data):
        (self.root / name).write_text(json.dumps(data), encoding="utf-8")

    def read(self, name="settings.json"):
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def test_switch_and_restore_prepend_common_settings_and_preserve_provider(self):
        for target, expected in (("settings_test.json", self.provider), ("settings.json", self.base)):
            ok, message = accounts.switch_account(target)
            self.assertTrue(ok, message)
            saved = self.read()
            self.assertEqual(list(saved)[:2], ["hooks", "permissions"])
            self.assertEqual(saved["env"], expected["env"])
            self.assertEqual(saved["model"], expected["model"])
            self.assertEqual(saved["hooks"]["SessionStart"], self.common["hooks"]["SessionStart"])
        self.assertFalse((self.root / "settings.json.bak").exists())

    def test_matching_groups_and_permissions_are_merged_without_duplicates(self):
        partial = {"hooks": {"SessionStart": [{"hooks": [handler("shared"), handler("extra")]}]},
                   "permissions": {"allow": ["Read", "Edit"], "deny": ["Bash(profile-deny)"]}}
        result = with_shared_settings(self.root, partial)
        self.assertEqual(result["hooks"]["SessionStart"], [{"hooks": [handler("shared"), handler("extra")]}])
        self.assertEqual(result["permissions"]["allow"], ["Read", "Edit"])
        self.assertEqual(result["permissions"]["deny"], ["Bash(shared-deny)", "Bash(profile-deny)"])
        self.assertEqual(with_shared_settings(self.root, result), result)
        self.assertNotIn("permissions", self.base)

    def test_already_active_account_refreshes_template(self):
        self.assertTrue(accounts.switch_account("settings_test.json")[0])
        self.common["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [handler("new")]}]
        self.write("settings-hooks.json", self.common)
        self.assertTrue(accounts.switch_account("settings_test.json")[0])
        self.assertIn("PreToolUse", self.read()["hooks"])
        first = self.read()
        self.assertTrue(accounts.switch_account("settings_test.json")[0])
        self.assertEqual(self.read(), first)

    def test_missing_active_settings_are_created_with_shared_hooks(self):
        for target in ("settings.json", "settings_test.json"):
            (self.root / "settings.json").unlink()
            self.assertTrue(accounts.switch_account(target)[0])
            self.assertEqual(self.read()["hooks"]["SessionStart"], self.common["hooks"]["SessionStart"])

    def test_invalid_template_or_profile_does_not_change_state(self):
        for filename in ("settings-hooks.json", "settings_test.json"):
            original = (self.root / filename).read_bytes()
            for broken in ("not JSON", "[]", '{"hooks":[]}'):
                (self.root / filename).write_text(broken)
                before = (self.root / "settings.json").read_bytes()
                ok, message = accounts.switch_account("settings_test.json")
                self.assertFalse(ok, message)
                self.assertEqual((self.root / "settings.json").read_bytes(), before)
                self.assertFalse((self.root / "settings.json.bak").exists())
                self.assertFalse((self.root / ".active-account").exists())
            (self.root / filename).write_bytes(original)

    def test_active_account_edit_keeps_common_settings_in_both_copies(self):
        self.assertTrue(accounts.switch_account("settings_test.json")[0])
        ok, message = accounts.write_account_config("settings_test.json", {"CUSTOM": "edited"}, {"model": "edited"})
        self.assertTrue(ok, message)
        for name in ("settings_test.json", "settings.json"):
            saved = self.read(name)
            self.assertEqual(list(saved)[:2], ["hooks", "permissions"])
            self.assertEqual(saved["env"], {"CUSTOM": "edited"})

    def test_no_template_keeps_legacy_behavior(self):
        (self.root / "settings-hooks.json").unlink()
        self.assertTrue(accounts.switch_account("settings_test.json")[0])
        self.assertEqual(self.read(), self.provider)

    def test_template_cannot_be_selected_as_account(self):
        self.assertFalse(accounts.switch_account("settings-hooks.json")[0])

    def test_atomic_write_failure_leaves_valid_settings(self):
        before = (self.root / "settings.json").read_bytes()
        with mock.patch.object(accounts.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(accounts.switch_account("settings.json")[0])
        self.assertEqual((self.root / "settings.json").read_bytes(), before)
        self.assertEqual(list(self.root.glob(".env-*")), [])


if __name__ == "__main__":
    unittest.main()
