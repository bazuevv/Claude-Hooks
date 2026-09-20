"""Cross-platform regressions; run directly or with unittest discovery."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import codex_app_server
import codex_bridge_manager
import host_platform

HERE = Path(__file__).resolve().parent


class HostPlatformTests(unittest.TestCase):
    def test_vscode_paths_on_all_platforms(self):
        for platform, environment, base in (
            ("darwin", {}, "/home/test/Library/Application Support"),
            ("linux", {}, "/home/test/.config"),
            ("linux", {"XDG_CONFIG_HOME": "/custom config"}, "/custom config"),
            ("win32", {"APPDATA": "/roaming data"}, "/roaming data"),
            ("win32", {}, "/home/test/AppData/Roaming"),
        ):
            with self.subTest(platform=platform, environment=environment), \
                    mock.patch.object(sys, "platform", platform), \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(os.path, "expanduser", return_value="/home/test"):
                self.assertEqual(host_platform.vscode_data_dirs(), [
                    os.path.join(base, "Code"), os.path.join(base, "Code - Insiders"),
                ])

    def test_macos_process_inspection_and_ownership(self):
        command = '/some Python ' + codex_bridge_manager.BRIDGE_SCRIPT
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, command + "\n")) as run:
            self.assertEqual(codex_bridge_manager._owned_pid(42), 42)
            self.assertEqual(run.call_args.args[0],
                             ["/bin/ps", "-ww", "-p", "42", "-o", "command="])
            run.return_value = subprocess.CompletedProcess([], 0, "unrelated process")
            self.assertIsNone(codex_bridge_manager._owned_pid(42))
            run.return_value = subprocess.CompletedProcess([], 1, command)
            self.assertIsNone(codex_bridge_manager._owned_pid(42))
            run.side_effect = subprocess.TimeoutExpired("ps", 5)
            self.assertIsNone(codex_bridge_manager._owned_pid(42))

    def test_linux_process_inspection(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data=b"python\0/hook.py\0")):
            self.assertEqual(host_platform.process_command(42), "python /hook.py ")

    def test_windows_process_inspection(self):
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps('python.exe "C:/a b/hook.py"'))) as run:
            self.assertEqual(host_platform.process_command(42), 'python.exe "C:/a b/hook.py"')
            self.assertEqual(run.call_args.args[0][0], "powershell.exe")
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_invalid_pids_never_run_commands(self):
        with mock.patch.object(subprocess, "run") as run:
            for pid in (None, True, 0, -1, 1, "42"):
                self.assertIsNone(host_platform.process_command(pid))
            run.assert_not_called()

    def test_codex_binary_platform_and_architecture(self):
        for platform, machine, os_name, expected in (
            ("darwin", "arm64", "posix", "macos-aarch64/codex"),
            ("darwin", "x86_64", "posix", "macos-x86_64/codex"),
            ("linux", "aarch64", "posix", "linux-aarch64/codex"),
            ("linux", "x86_64", "posix", "linux-x86_64/codex"),
            ("win32", "AMD64", "nt", "windows-x86_64/codex.exe"),
            ("win32", "ARM64", "nt", "windows-aarch64/codex.exe"),
        ):
            with self.subTest(platform=platform, machine=machine), \
                    mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(codex_app_server, "os", types.SimpleNamespace(name=os_name, path=os.path)), \
                    mock.patch.object(codex_app_server.platform, "machine", return_value=machine), \
                    mock.patch.object(codex_app_server.glob, "glob", side_effect=lambda pattern: [pattern]), \
                    mock.patch.object(os.path, "getmtime", return_value=1):
                candidates = codex_app_server._extension_binary_candidates()
                self.assertTrue(any(path.endswith(expected) for path in candidates))
                if platform == "darwin":
                    self.assertFalse(any("/linux-" in path for path in candidates))

    def test_rules_without_root_claude_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hooks = root / ".claude" / "hooks"
            hooks.mkdir(parents=True)
            shutil.copyfile(HERE / "project-work-rules.py", hooks / "project-work-rules.py")
            (root / ".claude" / "Readme.md").write_text("Windows, Linux и macOS", encoding="utf-8")
            result = subprocess.run([sys.executable, str(hooks / "project-work-rules.py")],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"],
                             "Windows, Linux и macOS")

    @unittest.skipIf(sys.version_info < (3, 11), "Patchers require Python 3.11+ (tomllib)")
    def test_patchers_use_macos_paths(self):
        with mock.patch.object(sys, "platform", "darwin"):
            modules = []
            for name in ("patch-claude-webview", "patch-extension-settings"):
                spec = importlib.util.spec_from_file_location(name, HERE / (name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                modules.append(module)
        self.assertIn(os.path.expanduser("~/Library/Application Support/Code/User/settings.json"),
                      modules[0].USER_SETTINGS_PATHS)
        self.assertIn(os.path.expanduser("~/Library/Application Support/Code/CachedProfilesData/*/extensions.user.cache"),
                      modules[1].CACHE_GLOBS)


@unittest.skipUnless(shutil.which("bash"), "Bash is required for the hook dispatcher")
class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude hooks тест ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hooks = self.root / "hooks with spaces"
        self.hooks.mkdir()
        shutil.copyfile(HERE / "_run.sh", self.hooks / "_run.sh")
        shutil.copyfile(HERE / "python_probe.sh", self.hooks / "python_probe.sh")
        (self.hooks / "probe.py").write_text(
            "import sys\nprint(sys.argv[1])\nprint(sys.stdin.read())\nsys.exit(7)\n", encoding="utf-8")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.executable("dirname", 'exec /usr/bin/dirname "$@"')
        self.executable("uname", "echo Linux")
        # Simulate a fresh user's home so installed uv Python cannot escape the fixture.
        self.environment = dict(os.environ, PATH=str(self.bin), HOME=str(self.root))
        self.environment.pop("CLAUDE_HOOK_PYTHON", None)

    def executable(self, name, script):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def interpreter(self, name):
        return self.executable(name,
            'if [ "$1" = "-3" ]; then shift; fi\n'
            'if [ "$1" = "-c" ]; then\n'
            '  case "$2" in *"(3, 11)"*) exit 0;; *) exit 99;; esac\n'
            'fi\nexec ' + shlex.quote(sys.executable) + ' "$@"')

    def dispatch(self):
        return subprocess.run([shutil.which("bash"), str(self.hooks / "_run.sh"),
                               "probe.py", "argument with пробелы"],
                              input='{"prompt":"Привет"}', capture_output=True,
                              text=True, env=self.environment, cwd=self.root)

    def assert_probe(self, result):
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, 'argument with пробелы\n{"prompt":"Привет"}\n')

    def test_skips_old_python_and_store_stub(self):
        self.executable("python3", "exit 1")
        self.executable("python", "exit 49")
        self.interpreter("python3.11")
        self.assert_probe(self.dispatch())

    def test_mac_never_executes_apple_python_shim_through_alias(self):
        self.executable("uname", "echo Darwin")
        for command in ("basename", "readlink"):
            self.executable(command, 'exec /usr/bin/' + command + ' "$@"')
        (self.bin / "python3").symlink_to("/usr/bin/python3")
        self.interpreter("python3.13")
        self.assert_probe(self.dispatch())

    def test_windows_launcher(self):
        self.executable("python3", "exit 49")
        self.interpreter("py")
        self.assert_probe(self.dispatch())

    def test_explicit_interpreter_with_spaces(self):
        self.environment["CLAUDE_HOOK_PYTHON"] = str(self.interpreter("custom python"))
        self.assert_probe(self.dispatch())

    def test_missing_interpreter_reports_requirement(self):
        result = self.dispatch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.11+", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_invalid_override_fails_explicitly(self):
        self.interpreter("python3")
        self.environment["CLAUDE_HOOK_PYTHON"] = str(self.bin / "missing")
        result = self.dispatch()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CLAUDE_HOOK_PYTHON", result.stderr)

    def test_installed_python_and_paths_are_used_without_shell_evaluation(self):
        state = self.root / "install-state"
        state.mkdir()
        custom = self.interpreter("installed python")
        (state / "python-path.txt").write_text(str(custom) + "\n")
        # PATH lines are data, including shell metacharacters and Unicode.
        extra = self.root / "tools $(touch UNEXPECTED) тест"
        extra.mkdir()
        (state / "bin-paths.txt").write_text(str(extra) + "\n")
        (self.hooks / "probe.py").write_text(
            "import os, sys\nprint(os.environ['PATH'].split(os.pathsep)[0])\n"
            "print(sys.stdin.read())\nsys.exit(7)\n")
        result = self.dispatch()
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], str(extra))
        self.assertIn('Привет', result.stdout)
        self.assertFalse((self.root / "UNEXPECTED").exists())

    def test_stale_installed_python_falls_back(self):
        state = self.root / "install-state"
        state.mkdir()
        (state / "python-path.txt").write_text("/missing/python\n")
        self.interpreter("python3")
        self.assert_probe(self.dispatch())


if __name__ == "__main__":
    unittest.main()
