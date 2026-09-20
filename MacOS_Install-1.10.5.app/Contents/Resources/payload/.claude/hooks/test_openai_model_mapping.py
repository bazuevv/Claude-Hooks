"""Regression coverage for Claude role aliases backed by Codex models."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest import mock

import account_switcher as accounts


CATALOG = [
    {"id": "gpt-5.6-sol", "isDefault": True},
    {"id": "gpt-6-astra"},
    {"id": "gpt-5.6-terra"},
    {"id": "gpt-5.6-luna"},
    {"id": "gpt-5.5"},
]
EXPECTED = dict(zip(accounts.OPENAI_ROLE_KEYS,
                    ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra")))


class OpenAIModelMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.profile = self.claude / "settings_openai.json"
        self.active = self.claude / "settings.json"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            "CLAUDE_DIR": str(self.claude), "SETTINGS_FILE": str(self.active),
            "BACKUP_FILE": str(self.active) + ".bak",
            "ACTIVE_MARKER": str(self.claude / ".active-account"),
        }.items():
            self.stack.enter_context(mock.patch.object(accounts, name, value))

    def write_profile(self, path, **extra):
        data = {
            "model": "gpt-6-astra", "hooks": {"Stop": []},
            "env": {
                "ANTHROPIC_BASE_URL": f"http://{accounts.codex_bridge_manager.BRIDGE_HOST}:"
                                      f"{accounts.codex_bridge_manager.BRIDGE_PORT}",
                "ANTHROPIC_AUTH_TOKEN": "test-local",
                "ANTHROPIC_MODEL": "gpt-6-astra",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "258400",
                **dict.fromkeys(accounts.OPENAI_ROLE_KEYS, "gpt-6-astra"),
            },
            **extra,
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def read(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def test_capability_order_is_independent_of_catalog_order_and_default(self):
        self.assertEqual(accounts.openai_role_models(CATALOG), EXPECTED)
        self.assertEqual(accounts.openai_role_models(list(reversed(CATALOG))), EXPECTED)

    def test_sync_repairs_both_files_preserves_other_settings_and_is_idempotent(self):
        before = self.write_profile(self.profile)
        active_before = self.write_profile(self.active, language="russian")
        changed = accounts.sync_openai_models(CATALOG)
        self.assertEqual(set(changed), {str(self.profile), str(self.active)})
        for path, original in ((self.profile, before), (self.active, active_before)):
            original["env"].update(EXPECTED)
            self.assertEqual(self.read(path), original)
        with mock.patch.object(accounts, "_write_json_atomic") as write:
            self.assertEqual(accounts.sync_openai_models(CATALOG), [])
            write.assert_not_called()

    def test_other_active_provider_is_untouched(self):
        self.write_profile(self.profile)
        self.active.write_text('{"model":"sonnet","env":{}}', encoding="utf-8")
        before = self.active.read_bytes()
        self.assertEqual(accounts.sync_openai_models(CATALOG), [str(self.profile)])
        self.assertEqual(self.active.read_bytes(), before)

    def test_incomplete_or_unavailable_catalog_keeps_working_settings(self):
        self.write_profile(self.profile)
        before = self.profile.read_bytes()
        for catalog in ([], CATALOG[:2], [{"id": "unknown"}], "bad",
                        [*CATALOG[:2], {"id": "gpt-5.6-terra", "hidden": True}]):
            self.assertEqual(accounts.sync_openai_models(catalog), [])
            self.assertEqual(self.profile.read_bytes(), before)
        with mock.patch.object(accounts.codex_bridge_manager, "account_snapshot", return_value=None):
            self.assertEqual(accounts.sync_openai_models(), [])

    def test_switch_repairs_profile_before_activating_it(self):
        self.write_profile(self.profile)
        self.active.write_text('{"model":"sonnet"}', encoding="utf-8")
        with mock.patch.object(accounts.codex_bridge_manager, "account_snapshot",
                               return_value={"models": CATALOG}), \
                mock.patch.object(accounts, "log_account_event"):
            ok, message = accounts.switch_account("settings_openai.json")
        self.assertTrue(ok, message)
        self.assertEqual({key: self.read(self.active)["env"][key] for key in EXPECTED}, EXPECTED)
        self.assertEqual(self.read(Path(accounts.BACKUP_FILE)), {"model": "sonnet"})

    def test_setup_creates_distinct_roles_and_repairs_existing_profile(self):
        setup = runpy.run_path(str(Path(__file__).with_name("setup-openai-account.py")))
        client_type = setup["CodexAppServerClient"]
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.object(client_type, "__enter__") as enter, \
                mock.patch.object(client_type, "__exit__"), \
                mock.patch.object(accounts.codex_bridge_manager, "ensure", return_value=(True, "ok")), \
                contextlib.redirect_stdout(io.StringIO()):
            enter.return_value.snapshot.return_value = {
                "account": {"account": {"type": "chatgpt"}}, "models": CATALOG,
            }
            setup["main"]()
            self.assertEqual({key: self.read(self.profile)["env"][key] for key in EXPECTED}, EXPECTED)
            before = self.write_profile(self.profile, language="russian")
            setup["main"]()
            before["env"].update(EXPECTED)
            self.assertEqual(self.read(self.profile), before)

    def test_setup_prepends_shared_hooks_to_new_profile(self):
        shared = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "shared"}]}]}}
        (self.claude / "settings-hooks.json").write_text(json.dumps(shared))
        setup = runpy.run_path(str(Path(__file__).with_name("setup-openai-account.py")))
        client_type = setup["CodexAppServerClient"]
        with mock.patch.object(Path, "home", return_value=self.home), \
                mock.patch.object(client_type, "__enter__") as enter, \
                mock.patch.object(client_type, "__exit__"), \
                mock.patch.object(accounts.codex_bridge_manager, "ensure", return_value=(True, "ok")), \
                contextlib.redirect_stdout(io.StringIO()):
            enter.return_value.snapshot.return_value = {
                "account": {"account": {"type": "chatgpt"}}, "models": CATALOG,
            }
            setup["main"]()
        saved = self.read(self.profile)
        self.assertEqual(list(saved)[0], "hooks")
        self.assertEqual(saved["hooks"], shared["hooks"])


if __name__ == "__main__":
    unittest.main()
