"""Run installed hooks from unrelated projects without a project .claude folder."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import deploy_hooks


class GlobalHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="global hooks тест ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        deploy_hooks.deploy_global(Path(__file__).resolve().parent.parent, self.home)
        self.claude = self.home / ".claude"
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "PYTHONDONTWRITEBYTECODE": "1", "CLAUDE_HOOK_PYTHON": sys.executable}
        self.projects = [self.root / "проект A", self.root / "project B"]
        for project in self.projects:
            project.mkdir()
            (project / "CLAUDE.md").write_text(project.name, encoding="utf-8")

    def run_hook(self, name, project, data):
        return subprocess.run([sys.executable, "-B", str(self.claude / "hooks" / name)],
                              input=json.dumps(data), capture_output=True, text=True,
                              cwd=project, env={**self.env, "CLAUDE_PROJECT_DIR": str(project)}, check=True).stdout

    def test_rules_follow_current_project(self):
        for project in self.projects:
            result = json.loads(self.run_hook("project-work-rules.py", project, {"cwd": str(project)}))
            self.assertEqual(result["hookSpecificOutput"]["additionalContext"], project.name)
            self.assertFalse((project / ".claude").exists())

    def test_bypass_markers_are_shared_with_server_but_isolated_by_session(self):
        sid = "5fca8a32-40e9-46ec-963c-b0d3dd44634d"
        a, b = self.projects
        self.run_hook("bypass-magic-word.py", a, {"session_id": sid, "prompt": "да!"})
        result = json.loads(self.run_hook("bypass-check.py", a, {"session_id": sid}))
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(self.run_hook("bypass-check.py", b, {"session_id": "different-session"}), "")
        marker = self.claude / "hooks-runtime/bypass" / sid
        self.assertTrue(marker.exists())
        self.run_hook("bypass-cleanup.py", a, {"session_id": sid})
        self.assertFalse(marker.exists())
        self.assertFalse((a / ".claude").exists())
        self.assertFalse((b / ".claude").exists())

    def test_patchers_and_server_read_installed_resources(self):
        code = '''
import importlib.util
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import hook_paths
assert hook_paths.GLOBAL_INSTALL
assert hook_paths.ROOT == (Path.home() / ".claude").resolve()
modules = {}
for name in ("patch-claude-webview", "patch-extension-settings", "localize", "http-server"):
    spec = importlib.util.spec_from_file_location(name, Path(sys.argv[1]) / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    modules[name] = module
for name in ("patch-claude-webview", "patch-extension-settings"):
    assert Path(modules[name].CANONICAL_CONFIG).is_relative_to(hook_paths.ROOT)
assert Path(modules["localize"].CONFIG_PATH).is_relative_to(hook_paths.ROOT)
assert Path(modules["http-server"].LOGS_DIR) == hook_paths.RUNTIME
assert Path(modules["patch-claude-webview"]._pid_file_path()).parent == hook_paths.RUNTIME
'''
        for project in self.projects:
            result = subprocess.run([sys.executable, "-B", "-c", code, str(self.claude / "hooks")],
                           cwd=project, env={**self.env, "CLAUDE_PROJECT_DIR": str(project)},
                           capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
