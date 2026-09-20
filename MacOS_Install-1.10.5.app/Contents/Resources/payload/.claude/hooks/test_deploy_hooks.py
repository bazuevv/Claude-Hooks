import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import deploy_hooks


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hooks install тест ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in deploy_hooks.PAYLOAD_NAMES:
            item = self.source / name
            if name in ("hooks", "patches"):
                item.mkdir()
            else:
                item.write_text("payload", encoding="utf-8")
        self.settings = {"hooks": {"SessionStart": [{"hooks": [{"command": "our hook"}]}]}}
        (self.source / "settings-hooks.json").write_text(json.dumps(self.settings))
        (self.source / "hooks/example.py").write_text("print('new')")
        (self.source / "patches/claude-custom-config.toml").write_text("enabled = true")
        self.project = self.root / "Проект с пробелами"
        self.project.mkdir()

    def test_clean_distribution_excludes_local_state_and_nested_installer(self):
        for name in ("hooks-runtime", "usage-history", "install-state", "MacOS_Install.app"):
            (self.source / name).mkdir()
            (self.source / name / "private.txt").write_text("private")
        (self.source / "settings.local.json").write_text("private")
        (self.source / "hooks/__pycache__").mkdir()
        (self.source / "hooks/__pycache__/secret.pyc").write_text("private")
        target = self.root / "payload"
        deploy_hooks.copy_payload(self.source, target)
        self.assertEqual(set(path.name for path in target.iterdir()), set(deploy_hooks.PAYLOAD_NAMES) | {"settings.json"})
        self.assertFalse((target / "hooks/__pycache__").exists())

    def test_protected_template_survives_project_deduplication(self):
        protected = {"hooks": self.settings["hooks"], "permissions": {"allow": ["old"]}}
        (self.source / deploy_hooks.SOURCE_TEMPLATE_NAME).write_text(json.dumps(protected))
        (self.source / "settings-hooks.json").write_text(json.dumps({"permissions": {"allow": ["current"]}}))
        target = self.root / "protected-payload"
        deploy_hooks.copy_payload(self.source, target)
        result = json.loads((target / "settings-hooks.json").read_text())
        self.assertEqual(result["hooks"], self.settings["hooks"])
        self.assertEqual(result["permissions"], {"allow": ["current"]})

    def test_new_project_gets_hooks_and_dependency_paths(self):
        (self.source / "install-state").mkdir()
        (self.source / "install-state/python-path.txt").write_text("/user/python\n")
        result = deploy_hooks.deploy(self.source, self.project)
        self.assertIsNone(result["backup"])
        self.assertEqual((self.project / ".claude/install-state/python-path.txt").read_text(), "/user/python\n")
        self.assertTrue((self.project / ".claude/hooks/example.py").exists())

    def test_update_backs_up_and_preserves_settings_and_extra_files(self):
        existing = self.project / ".claude"
        (existing / "patches").mkdir(parents=True)
        previous = {"env": {"CUSTOM": "keep"}, "hooks": {"SessionStart": [{"hooks": [{"command": "user hook"}]}]}}
        (existing / "settings.json").write_text(json.dumps(previous))
        (existing / "my-file.txt").write_text("keep me")
        (existing / "patches/claude-custom-config.toml").write_text("enabled = false")
        result = deploy_hooks.deploy(self.source, self.project)
        backup = Path(result["backup"])
        self.assertEqual(json.loads((backup / "settings.json").read_text()), previous)
        updated = json.loads((existing / "settings.json").read_text())
        self.assertEqual(updated["env"], previous["env"])
        self.assertEqual(len(updated["hooks"]["SessionStart"]), 2)
        self.assertEqual((existing / "my-file.txt").read_text(), "keep me")
        self.assertEqual((existing / "patches/claude-custom-config.toml").read_text(), "enabled = false")
        deploy_hooks.deploy(self.source, self.project)
        self.assertEqual(json.loads((existing / "settings.json").read_text()), updated)

    def test_invalid_existing_settings_leave_project_unchanged(self):
        target = self.project / ".claude"
        target.mkdir()
        (target / "settings.json").write_text("invalid")
        with self.assertRaises(ValueError):
            deploy_hooks.deploy(self.source, self.project)
        self.assertEqual((target / "settings.json").read_text(), "invalid")
        self.assertEqual(list(self.project.glob(".claude.backup-*")), [])

    def test_failure_at_commit_restores_original_directory(self):
        target = self.project / ".claude"
        target.mkdir()
        (target / "keep.txt").write_text("old")
        rename = os.rename
        def fail_commit(source, destination):
            if Path(source).parent.name.startswith(".claude-install-"):
                raise OSError("simulated filesystem failure")
            return rename(source, destination)
        with mock.patch.object(deploy_hooks.os, "rename", side_effect=fail_commit):
            with self.assertRaises(OSError):
                deploy_hooks.deploy(self.source, self.project)
        self.assertEqual((target / "keep.txt").read_text(), "old")
        self.assertFalse((target / "hooks").exists())

    @unittest.skipIf(os.name == "nt", "Creating Windows symlinks needs privileges")
    def test_symlink_collision_does_not_write_outside_project(self):
        target = self.project / ".claude"
        target.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (target / "hooks").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            deploy_hooks.deploy(self.source, self.project)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((target / "hooks").is_symlink())

    def test_selecting_claude_itself_is_rejected(self):
        target = self.project / ".claude"
        target.mkdir()
        with self.assertRaises(ValueError):
            deploy_hooks.deploy(self.source, target)

    def test_global_install_preserves_accounts_sessions_preferences_and_backup(self):
        home = self.root / "home"
        target = home / ".claude"
        (target / "projects").mkdir(parents=True)
        (target / "projects/private.jsonl").write_text("session")
        (target / "settings_api.json").write_text('{"env":{"SECRET":"keep"}}')
        original = {"env": {"TOKEN": "keep"}, "model": "provider-model"}
        (target / "settings.json").write_text(json.dumps(original))
        (target / "message-costs.toml").write_text("keep = true")
        result = deploy_hooks.deploy_global(self.source, home)
        saved = json.loads((target / "settings.json").read_text())
        self.assertEqual(list(saved)[0], "hooks")
        self.assertEqual(saved["env"], original["env"])
        self.assertEqual((target / "projects/private.jsonl").read_text(), "session")
        self.assertEqual((target / "settings_api.json").read_text(), '{"env":{"SECRET":"keep"}}')
        self.assertEqual((target / "message-costs.toml").read_text(), "keep = true")
        self.assertEqual(json.loads((Path(result["backup"]) / "settings.json").read_text()), original)
        deploy_hooks.deploy_global(self.source, home)
        self.assertEqual(json.loads((target / "settings.json").read_text()), saved)

    def test_global_failure_restores_previous_scripts_and_settings(self):
        home = self.root / "home"
        target = home / ".claude"
        (target / "hooks").mkdir(parents=True)
        (target / "hooks/example.py").write_text("old")
        (target / "settings.json").write_text('{"model":"old"}')
        replace = os.replace
        def fail_settings(source, destination):
            if Path(destination) == target / "settings.json" and "backups" not in Path(source).parts:
                raise OSError("simulated settings failure")
            return replace(source, destination)
        with mock.patch.object(deploy_hooks.os, "replace", side_effect=fail_settings):
            with self.assertRaises(OSError):
                deploy_hooks.deploy_global(self.source, home)
        self.assertEqual((target / "hooks/example.py").read_text(), "old")
        self.assertEqual((target / "settings.json").read_text(), '{"model":"old"}')
        self.assertFalse((target / "settings-hooks.json").exists())

    def test_global_invalid_settings_do_not_install_any_scripts(self):
        home = self.root / "home"
        target = home / ".claude"
        target.mkdir(parents=True)
        (target / "settings.json").write_text("invalid")
        with self.assertRaises(ValueError):
            deploy_hooks.deploy_global(self.source, home)
        self.assertFalse((target / "hooks").exists())
        self.assertEqual((target / "settings.json").read_text(), "invalid")

    def test_global_and_project_payload_commands_use_their_installation(self):
        self.settings["hooks"]["SessionStart"][0]["hooks"][0]["command"] = 'bash "$HOME/.claude/hooks/_run.sh" example.py'
        (self.source / "settings-hooks.json").write_text(json.dumps(self.settings))
        for global_install, variable in ((True, "$HOME"), (False, "$CLAUDE_PROJECT_DIR")):
            destination = self.root / str(global_install)
            deploy_hooks.copy_payload(self.source, destination, global_install=global_install)
            saved = json.loads((destination / "settings.json").read_text())
            self.assertIn(variable, saved["hooks"]["SessionStart"][0]["hooks"][0]["command"])

    def api_profile(self):
        profile = {"env": {"ANTHROPIC_AUTH_TOKEN": "fixture-token", "ANTHROPIC_BASE_URL": "https://fixture.invalid"}}
        (self.source / deploy_hooks.DEFAULT_ACCOUNT).write_text(json.dumps(profile))
        return profile

    def test_fresh_global_install_activates_bundled_api_and_registers_hooks(self):
        profile = self.api_profile()
        home = self.root / "fresh-home"
        result = deploy_hooks.deploy_global(self.source, home)
        target = home / ".claude"
        active = json.loads((target / "settings.json").read_text())
        self.assertEqual(active["env"], profile["env"])
        self.assertEqual(active["hooks"], self.settings["hooks"])
        self.assertEqual((target / ".active-account").read_text(), deploy_hooks.DEFAULT_ACCOUNT)
        self.assertEqual(json.loads((target / "settings.json.bak").read_text()), {})
        self.assertEqual(result["activated_account"], deploy_hooks.DEFAULT_ACCOUNT)
        self.assertIsNone(deploy_hooks.deploy_global(self.source, home)["activated_account"])

    def test_existing_connection_is_never_replaced_but_inactive_profile_does_not_block_default(self):
        self.api_profile()
        for kind in ("other-profile", "active-provider", "oauth", "active-marker"):
            with self.subTest(kind=kind):
                home = self.root / kind
                target = home / ".claude"
                target.mkdir(parents=True)
                original = {"theme": "dark"}
                if kind == "other-profile":
                    (target / "settings_openai.json").write_text('{}')
                elif kind == "active-provider":
                    original["env"] = {"ANTHROPIC_AUTH_TOKEN": "existing-token"}
                elif kind == "oauth":
                    (target / ".credentials.json").write_text('{}')
                else:
                    (target / "settings_custom.json").write_text(json.dumps({
                        "env": {"ANTHROPIC_AUTH_TOKEN": "custom-token"},
                    }))
                    (target / ".active-account").write_text('settings_custom.json')
                (target / "settings.json").write_text(json.dumps(original))
                result = deploy_hooks.deploy_global(self.source, home)
                active = json.loads((target / "settings.json").read_text())
                self.assertTrue((target / deploy_hooks.DEFAULT_ACCOUNT).is_file())
                if kind == "other-profile":
                    self.assertEqual(result["activated_account"], deploy_hooks.DEFAULT_ACCOUNT)
                    self.assertEqual(active["env"], self.api_profile()["env"])
                    self.assertEqual((target / ".active-account").read_text(), deploy_hooks.DEFAULT_ACCOUNT)
                    self.assertEqual(json.loads((target / "settings.json.bak").read_text()), original)
                else:
                    self.assertIsNone(result["activated_account"])
                    self.assertEqual(active.get("env"), original.get("env"))

    def test_stale_backup_or_invalid_marker_does_not_leave_active_settings_unconfigured(self):
        profile = self.api_profile()
        for kind in ("backup", "invalid-marker"):
            with self.subTest(kind=kind):
                home = self.root / kind
                target = home / ".claude"
                target.mkdir(parents=True)
                original = {"theme": "dark"}
                (target / "settings.json").write_text(json.dumps(original))
                if kind == "backup":
                    (target / "settings.json.bak").write_text('{}')
                else:
                    (target / ".active-account").write_text('settings_missing.json')
                result = deploy_hooks.deploy_global(self.source, home)
                self.assertEqual(result["activated_account"], deploy_hooks.DEFAULT_ACCOUNT)
                active = json.loads((target / "settings.json").read_text())
                self.assertEqual(active["env"], profile["env"])

    def test_two_saved_profiles_activate_api_when_active_settings_have_no_connection(self):
        profile = self.api_profile()
        home = self.root / "two-profiles"
        target = home / ".claude"
        target.mkdir(parents=True)
        original = {"theme": "dark", "permissions": {"allow": ["Read"]}}
        (target / "settings.json").write_text(json.dumps(original))
        (target / "settings_openai.json").write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18925",
                    "ANTHROPIC_AUTH_TOKEN": "fixture-openai"},
        }))
        result = deploy_hooks.deploy_global(self.source, home)
        active = json.loads((target / "settings.json").read_text())
        self.assertEqual(result["activated_account"], deploy_hooks.DEFAULT_ACCOUNT)
        self.assertEqual(active["env"], profile["env"])
        self.assertEqual(active["theme"], "dark")
        self.assertTrue((target / "settings_openai.json").is_file())

    def test_existing_api_profile_is_preserved_including_case(self):
        self.api_profile()
        home = self.root / "home"
        target = home / ".claude"
        target.mkdir(parents=True)
        profile = {"env": {"ANTHROPIC_AUTH_TOKEN": "user-token"}}
        (target / "settings_api.json").write_text(json.dumps(profile))
        deploy_hooks.deploy_global(self.source, home)
        self.assertEqual(json.loads((target / "settings_api.json").read_text()), profile)
        self.assertEqual((target / ".active-account").read_text(), "settings_api.json")
        self.assertEqual(json.loads((target / "settings.json").read_text())["env"], profile["env"])

    def test_activation_failure_rolls_back_account_and_marker(self):
        self.api_profile()
        home = self.root / "home"
        target = home / ".claude"
        target.mkdir(parents=True)
        (target / "settings.json").write_text('{"theme":"dark"}')
        replace = os.replace
        def fail_settings(source, destination):
            if Path(destination) == target / "settings.json" and "backups" not in Path(source).parts:
                raise OSError("simulated commit failure")
            return replace(source, destination)
        with mock.patch.object(deploy_hooks.os, "replace", side_effect=fail_settings), self.assertRaises(OSError):
            deploy_hooks.deploy_global(self.source, home)
        self.assertEqual((target / "settings.json").read_text(), '{"theme":"dark"}')
        for name in (deploy_hooks.DEFAULT_ACCOUNT, ".active-account", "settings.json.bak"):
            self.assertFalse((target / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
