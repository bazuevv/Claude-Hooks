"""Installer regressions without downloading software or changing the host."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
import unittest
from unittest import mock

import install_dependencies as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def test_check_never_installs_missing_dependencies(self):
        for system in ("Darwin", "Linux", "Windows"):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as directory:
                job = installer.Installer(check=True, system=system, claude=directory)
                with mock.patch.object(installer, "check_python"), \
                        mock.patch.object(job, "probe", return_value=None), \
                        mock.patch.object(job, "codex_available", return_value=False), \
                        mock.patch.object(job, "install_package") as install, \
                        mock.patch.object(installer, "execute"), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(job.run(), 1)
                    install.assert_not_called()
                    self.assertFalse((Path(directory) / "install-state").exists())
                    self.assertIn("wpctl", job.errors) if system == "Linux" else self.assertNotIn("wpctl", job.errors)

    def test_installs_only_missing_and_verifies_afterwards(self):
        job = installer.Installer(system="Linux")
        with mock.patch.object(job, "probe", side_effect=[None, "/bin/node", "/bin/node"]), \
                mock.patch.object(job, "install_package") as install:
            self.assertEqual(job.ensure("node"), "/bin/node")
            self.assertEqual(job.ensure("node"), "/bin/node")
            install.assert_called_once_with("node")

    def test_failed_installation_is_not_reported_as_success(self):
        job = installer.Installer(system="Darwin")
        with mock.patch.object(job, "probe", return_value=None), \
                mock.patch.object(job, "install_package"):
            with self.assertRaisesRegex(RuntimeError, "still unavailable"):
                job.ensure("ffplay")

    def test_install_preflights_every_dependency_before_first_ensure(self):
        job = installer.Installer(system="Darwin")
        order = []
        with mock.patch.object(installer, "check_python"), \
                mock.patch.object(job, "report", side_effect=lambda **kwargs: order.append(("report", kwargs)) or 1), \
                mock.patch.object(job, "ensure", side_effect=lambda name: order.append(("ensure", name)) or "/program"), \
                mock.patch.object(job, "ensure_extension", return_value=True), \
                mock.patch.object(job, "codex_available", return_value=True), \
                mock.patch.object(job, "save_environment"), \
                mock.patch.object(installer.os, "access", return_value=True):
            self.assertEqual(job.run(), 0)
        self.assertEqual(order[0], ("report", {"include_python": False}))
        self.assertEqual(order[1], ("ensure", "bash"))

    def test_package_commands_for_each_os(self):
        cases = (
            ("Darwin", "brew", "ffplay", ["brew", "install", "ffmpeg"]),
            ("Windows", "winget", "bash", ["winget", "install", "--id", "Git.Git"]),
            ("Linux", "apt-get", "node", ["apt-get", "install", "-y", "nodejs"]),
            ("Linux", "dnf", "ffplay", ["dnf", "install", "-y", "ffmpeg-free"]),
            ("Linux", "pacman", "wpctl", ["pacman", "-S", "--needed", "--noconfirm", "wireplumber"]),
            ("Linux", "zypper", "node", ["zypper", "--non-interactive", "install", "nodejs22"]),
        )
        for system, manager, name, expected in cases:
            with self.subTest(manager=manager):
                job = installer.Installer(system=system)
                job.manager = manager
                with mock.patch.object(installer, "execute") as execute, \
                        mock.patch.object(job, "privileged") as privileged, \
                        mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"")):
                    job.install_package(name)
                commands = [call.args[0] for call in execute.call_args_list + privileged.call_args_list]
                self.assertTrue(any(command[:len(expected)] == expected for command in commands), commands)

    def test_apt_refreshes_index_only_once(self):
        job = installer.Installer(system="Linux")
        job.manager = "apt-get"
        with mock.patch.object(job, "privileged") as privileged:
            job.install_package("node")
            job.install_package("ffplay")
        self.assertEqual(privileged.call_args_list.count(mock.call(["apt-get", "update"])), 1)

    def test_extension_installation_is_verified_and_idempotent(self):
        job = installer.Installer(system="Darwin")
        installed = subprocess.CompletedProcess([], 0, "anthropic.claude-code\n")
        with mock.patch.object(installer, "extension_package_url", return_value="https://fixture.invalid/package"), mock.patch.object(installer, "download"), mock.patch.object(installer, "execute", side_effect=[
                subprocess.CompletedProcess([], 0, ""), installed, installed, installed]) as execute:
            job.ensure_extension("/path with spaces/code", "anthropic.claude-code")
            job.ensure_extension("/path with spaces/code", "anthropic.claude-code")
        self.assertEqual(sum("--install-extension" in call.args[0] for call in execute.call_args_list), 1)

    def test_environment_is_plain_text_and_not_saved_in_check_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            job = installer.Installer(system="Darwin", claude=directory)
            job.remember("/Applications/Path with spaces/тест/node")
            job.save_environment()
            state = Path(directory) / "install-state"
            self.assertEqual((state / "python-path.txt").read_text().strip(), sys.executable)
            self.assertIn("/Applications/Path with spaces/тест", (state / "bin-paths.txt").read_text())

    def test_probe_rejects_broken_executables(self):
        job = installer.Installer(system="Linux")
        with mock.patch.object(shutil, "which", return_value="/bin/node"), \
                mock.patch.object(subprocess, "run", side_effect=subprocess.CalledProcessError(1, "node")):
            self.assertIsNone(job.probe("node"))

    def test_probe_finds_valid_node_after_old_node(self):
        job = installer.Installer(system="Linux")
        with mock.patch.dict(os.environ, {"PATH": "/old:/new"}), \
                mock.patch.object(shutil, "which", side_effect=["/old/node", "/new/node"]), \
                mock.patch.object(subprocess, "run", side_effect=[
                    subprocess.CalledProcessError(1, "node"), subprocess.CompletedProcess([], 0)]):
            self.assertEqual(job.probe("node"), "/new/node")

    def test_old_python_is_rejected_before_module_check(self):
        with mock.patch.object(sys, "version_info", (3, 9, 6)), \
                mock.patch.object(installer.importlib, "import_module") as imports:
            with self.assertRaisesRegex(RuntimeError, "3.11"):
                installer.check_python()
            imports.assert_not_called()

    def test_missing_python_module_is_not_ignored(self):
        with mock.patch.object(sys, "version_info", (3, 13)), \
                mock.patch.object(installer.importlib, "import_module", side_effect=ImportError("sqlite3")):
            with self.assertRaises(ImportError):
                installer.check_python()

    def test_report_checks_remaining_dependencies_without_installing_python(self):
        with tempfile.TemporaryDirectory() as directory:
            job = installer.Installer(check=True, system="Darwin", claude=directory)
            output = io.StringIO()
            with mock.patch.dict(os.environ, {"CLAUDE_INSTALL_EVENTS": "1"}), \
                    contextlib.redirect_stdout(output), \
                    mock.patch.object(installer, "check_python", side_effect=RuntimeError("old Python")), \
                    mock.patch.object(job, "probe", return_value=None), \
                    mock.patch.object(job, "codex_available", return_value=False), \
                    mock.patch.object(installer, "installed_extensions_from_disk", return_value=set()), \
                    mock.patch.object(job, "install_package") as packages, \
                    mock.patch.object(job, "save_environment") as save, \
                    mock.patch.object(installer, "download") as download:
                self.assertEqual(job.report(), 1)
                packages.assert_not_called()
                save.assert_not_called()
                download.assert_not_called()
            events = [json.loads(line.removeprefix("@claude-installer "))
                      for line in output.getvalue().splitlines() if line.startswith("@claude-installer ")]
            states = {event["id"]: event["state"] for event in events}
            self.assertEqual(states["python"], "missing")
            self.assertEqual(states["node"], "missing")
            self.assertEqual(states["claude"], "missing")
            self.assertEqual(states["codex"], "missing")
            self.assertEqual(set(states), {"python", "bash", "node", "audio", "code", "claude", "codex"})
            self.assertFalse((Path(directory) / "install-state").exists())

    def test_preserved_extensions_are_checked_without_vscode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".vscode/extensions"
            root.mkdir(parents=True)
            for folder, publisher, name, target in (
                    ("anthropic.claude-code-1.0-darwin-x64", "Anthropic", "claude-code", "darwin-x64"),
                    ("openai.chatgpt-1.0-darwin-x64", "openai", "chatgpt", "darwin-x64"),
                    ("openai.chatgpt-1.0-win32-x64", "openai", "chatgpt", "win32-x64")):
                extension = root / folder
                extension.mkdir()
                (extension / "extension.js").write_text("", encoding="utf-8")
                (extension / "package.json").write_text(json.dumps({
                    "publisher": publisher, "name": name, "main": "extension.js",
                    "__metadata": {"targetPlatform": target},
                }), encoding="utf-8")
            found = installer.installed_extensions_from_disk(
                home=directory, system="Darwin", machine="x86_64")
            self.assertEqual(found, {"anthropic.claude-code", "openai.chatgpt"})

    def test_existing_macos_vscode_is_found_without_launching_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            applications = root / "Applications"
            system_copy = applications / "Visual Studio Code.app"
            system_copy.mkdir(parents=True)
            self.assertEqual(installer.existing_macos_vscode(home, applications), system_copy)
            user_copy = home / "Applications/Visual Studio Code.app"
            user_copy.mkdir(parents=True)
            self.assertEqual(installer.existing_macos_vscode(home, applications), user_copy)

    def test_incomplete_and_obsolete_extension_directories_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / ".vscode/extensions"
            root.mkdir(parents=True)
            obsolete = root / "anthropic.claude-code-1.0-darwin-x64"
            obsolete.mkdir()
            manifest = {"publisher": "anthropic", "name": "claude-code", "main": "extension.js",
                        "__metadata": {"targetPlatform": "darwin-x64"}}
            (obsolete / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
            (obsolete / "extension.js").write_text("", encoding="utf-8")
            (root / ".obsolete").write_text(json.dumps({obsolete.name: True}), encoding="utf-8")
            incomplete = root / "openai.chatgpt-1.0-darwin-x64"
            incomplete.mkdir()
            (incomplete / "package.json").write_text(json.dumps({
                "publisher": "openai", "name": "chatgpt", "main": "missing.js",
                "__metadata": {"targetPlatform": "darwin-x64"},
            }), encoding="utf-8")
            self.assertEqual(installer.installed_extensions_from_disk(
                home=directory, system="Darwin", machine="x86_64"), set())

    def test_report_uses_preserved_extensions_when_vscode_is_missing(self):
        job = installer.Installer(check=True, system="Darwin")
        probes = {"bash": "/bin/bash", "node": "/node", "ffplay": None, "code": None}
        with mock.patch.object(installer, "check_python"), \
                mock.patch.object(job, "probe", side_effect=lambda name: probes.get(name)), \
                mock.patch.object(installer, "installed_extensions_from_disk",
                                  return_value={"anthropic.claude-code", "openai.chatgpt"}), \
                mock.patch.object(job, "codex_available", return_value=True) as codex, \
                mock.patch.object(installer, "emit") as emit:
            self.assertEqual(job.report(), 1)  # VS Code itself is still missing.
        emit.assert_any_call("code", "missing", None)
        emit.assert_any_call("claude", "ready", None)
        codex.assert_called_once()
        self.assertNotIn(mock.call("codex", "missing", None), emit.call_args_list)

    def test_ready_report_does_not_write_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            job = installer.Installer(check=True, system="Darwin", claude=directory)
            with mock.patch.object(installer, "check_python"), \
                    mock.patch.object(job, "probe", return_value="/program"), \
                    mock.patch.object(job, "codex_available", return_value=True), \
                    mock.patch.object(installer, "execute", return_value=subprocess.CompletedProcess([], 0, "anthropic.claude-code\nopenai.chatgpt\n")), \
                    mock.patch.object(job, "save_environment") as save:
                self.assertEqual(job.report(), 0)
                save.assert_not_called()
            self.assertFalse((Path(directory) / "install-state").exists())

    def test_report_requires_codex_extension_even_with_working_cli(self):
        for system in ("Darwin", "Linux", "Windows"):
            for extension_present, cli_ready in ((False, True), (True, False), (True, True)):
                with self.subTest(system=system, extension=extension_present, cli=cli_ready):
                    job = installer.Installer(check=True, system=system)
                    extensions = "anthropic.claude-code\n" + ("OpenAI.ChatGPT\n" if extension_present else "")
                    with mock.patch.object(installer, "check_python"), \
                            mock.patch.object(job, "probe", return_value="/program"), \
                            mock.patch.object(job, "codex_available", return_value=cli_ready) as cli, \
                            mock.patch.object(installer, "execute", return_value=subprocess.CompletedProcess([], 0, extensions)) as execute, \
                            mock.patch.object(installer, "emit") as emit:
                        self.assertEqual(job.report(), 0 if extension_present and cli_ready else 1)
                    self.assertFalse(any("--install-extension" in call.args[0] for call in execute.call_args_list))
                    if not extension_present:
                        cli.assert_not_called()
                        emit.assert_any_call("codex", "missing", None)

    def test_codex_extension_is_required_in_check_and_install_on_every_os(self):
        for system in ("Darwin", "Linux", "Windows"):
            for check in (True, False):
                for extension_present in (True, False):
                    with self.subTest(system=system, check=check, extension=extension_present):
                        job = installer.Installer(check=check, system=system)
                        extensions = {"anthropic.claude-code"}
                        if extension_present:
                            extensions.add("openai.chatgpt")

                        def execute(command, **kwargs):
                            if "--install-extension" in command:
                                extensions.add(Path(command[-1]).name.removesuffix(".vsix"))
                            return subprocess.CompletedProcess(command, 0, "\n".join(extensions))

                        with mock.patch.object(installer, "check_python"), \
                                mock.patch.object(job, "report", return_value=1), \
                                mock.patch.object(job, "ensure", return_value="/program"), \
                                mock.patch.object(job, "codex_available", return_value=True) as cli, \
                                mock.patch.object(installer, "extension_package_url", return_value="https://fixture.invalid/package"), mock.patch.object(installer, "download"), \
                                mock.patch.object(installer, "execute", side_effect=execute) as commands, \
                                mock.patch.object(job, "save_environment") as save, \
                                contextlib.redirect_stderr(io.StringIO()):
                            self.assertEqual(job.run(), 1 if check and not extension_present else 0)
                        installs = [call.args[0] for call in commands.call_args_list if "--install-extension" in call.args[0]]
                        if not check and not extension_present:
                            self.assertEqual(len(installs), 1)
                            self.assertEqual(Path(installs[0][-1]).name.removesuffix(".vsix"), "openai.chatgpt")
                        else:
                            self.assertEqual(installs, [])
                        if check:
                            save.assert_not_called()
                        if check and not extension_present:
                            self.assertIn("openai.chatgpt", job.errors)
                            cli.assert_not_called()
                        else:
                            cli.assert_called_once()

    def test_report_flags_cannot_record_environment(self):
        with mock.patch.object(installer.Installer, "report", return_value=0), \
                mock.patch.object(installer.Installer, "save_environment") as save:
            self.assertEqual(installer.main(["--report", "--prepare"]), 0)
            save.assert_not_called()
            self.assertEqual(installer.main(["--prepare"]), 0)
            save.assert_called_once()

    def test_report_probe_error_is_distinct_from_missing_dependency(self):
        with mock.patch.object(installer.Installer, "report", side_effect=OSError("failed")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(installer.main(["--report"]), 2)

    def test_report_code_checks_only_vscode(self):
        job = installer.Installer(check=True, system="Darwin")
        with mock.patch.object(job, "probe", return_value="/Applications/Visual Studio Code.app/code") as probe, \
                mock.patch.object(installer, "emit") as emit:
            self.assertEqual(job.report_code(), 0)
        probe.assert_called_once_with("code")
        emit.assert_called_once_with("code", "ready", "/Applications/Visual Studio Code.app/code")

    def test_report_code_mode_does_not_run_full_report(self):
        with mock.patch.object(installer.Installer, "report_code", return_value=0) as code, \
                mock.patch.object(installer.Installer, "report") as report:
            self.assertEqual(installer.main(["--report-code"]), 0)
        code.assert_called_once()
        report.assert_not_called()

    def test_global_deployment_requires_successful_dependency_installation(self):
        for result in (0, 1):
            with self.subTest(result=result), \
                    mock.patch.object(installer.Installer, "run", return_value=result), \
                    mock.patch("deploy_hooks.initialize_global"), \
                    mock.patch("deploy_hooks.deploy_global", return_value={"scope": "user", "installed": "/fixture/.claude"}) as deploy:
                self.assertEqual(installer.main(["--global"]), result)
                if result == 0:
                    deploy.assert_called_once_with(installer.CLAUDE)
                else:
                    deploy.assert_not_called()

    def test_global_install_cannot_be_combined_with_read_only_report(self):
        with mock.patch("deploy_hooks.deploy_global") as deploy, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.main(["--global", "--report"])
            deploy.assert_not_called()

    def test_mac_node_install_checks_hash_and_extracts_only_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            archive = io.BytesIO()
            filename = "node-v22.1.0-darwin-x64.tar.gz"
            with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                for name, data in (("node-v22.1.0-darwin-x64/bin/node", b"node binary"),
                                   ("../../unwanted", b"must not extract")):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    bundle.addfile(entry, io.BytesIO(data))
            content = archive.getvalue()
            digest = hashlib.sha256(content).hexdigest()
            def download(url, destination, dependency=None):
                Path(destination).write_bytes((digest + "  " + filename + "\n").encode()
                                              if url.endswith("SHASUMS256.txt") else content)
            job = installer.Installer(system="Darwin")
            with mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.object(installer.platform, "machine", return_value="x86_64"), \
                    mock.patch.object(installer, "download", side_effect=download), \
                    mock.patch.object(installer, "execute"):
                job.install_macos_tool("node")
            binary = home / "Library/Application Support/ClaudeHooks/tools/node/bin/node"
            self.assertEqual(binary.read_bytes(), b"node binary")
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertFalse((home / "unwanted").exists())

    def test_mac_node_bad_checksum_does_not_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            def download(url, destination, dependency=None):
                Path(destination).write_bytes(("0" * 64 + "  node-v22.1.0-darwin-arm64.tar.gz\n").encode()
                                              if url.endswith("SHASUMS256.txt") else b"corrupt")
            job = installer.Installer(system="Darwin")
            with mock.patch.object(Path, "home", return_value=home), \
                    mock.patch.object(installer.platform, "machine", return_value="arm64"), \
                    mock.patch.object(installer, "download", side_effect=download), \
                    mock.patch.object(installer, "execute") as execute:
                with self.assertRaisesRegex(RuntimeError, "checksum"):
                    job.install_macos_tool("node")
                execute.assert_not_called()
            self.assertFalse((home / "Library").exists())


@unittest.skipUnless(shutil.which("bash"), "Bash required")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="installer тест ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude = self.root / ".claude"
        (self.claude / "hooks").mkdir(parents=True)
        shutil.copyfile(installer.CLAUDE / "install.sh", self.claude / "install.sh")
        shutil.copyfile(installer.CLAUDE / "hooks/python_probe.sh", self.claude / "hooks/python_probe.sh")
        (self.claude / "hooks/_run.sh").write_text("#!/bin/bash\nexit 1\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.environment = dict(os.environ, PATH=str(self.bin) + ":/usr/bin:/bin", HOME=str(self.root))
        self.environment.pop("CLAUDE_HOOK_PYTHON", None)
        self.trace = self.root / "trace"
        self.executable("uname", "echo Linux")
        self.executable("uv", 'echo unexpected > ' + shlex.quote(str(self.trace)) + '\nexit 99')

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
        return path

    def run_installer(self, *args):
        return subprocess.run([shutil.which("bash"), str(self.claude / "install.sh"), *args],
                              env=self.environment, capture_output=True, text=True, cwd=self.root)

    def test_read_only_mode_does_not_bootstrap(self):
        result = self.run_installer("--check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.11+", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_missing_python_is_installed_and_selected(self):
        python = self.executable("test-python", 'echo "$*" >> ' + shlex.quote(str(self.trace)))
        self.executable("uv", 'echo "$*" >> ' + shlex.quote(str(self.trace)) + '\n'
                        'if [ "$2" = "find" ]; then printf "%s\\n" ' + shlex.quote(str(python)) + '; fi')
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self.trace.read_text()
        self.assertIn("python install 3.13", trace)
        self.assertIn("python find --managed-python 3.13", trace)
        self.assertIn("--probe-python", trace)

    def test_invalid_options_do_not_install(self):
        self.assertEqual(self.run_installer("--unknown").returncode, 2)
        self.assertFalse(self.trace.exists())

    def test_clean_mac_report_skips_apple_python_shim_and_does_not_install(self):
        self.executable("uname", "echo Darwin")
        (self.bin / "python3").symlink_to("/usr/bin/python3")
        (self.bin / "python").symlink_to("/usr/bin/python3")
        result = self.run_installer("--report")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('"id":"python","state":"missing"', result.stdout)
        events = [json.loads(line.removeprefix("@claude-installer "))
                  for line in result.stdout.splitlines() if line.startswith("@claude-installer ")]
        states = {event["id"]: event["state"] for event in events}
        self.assertEqual(states["bash"], "ready")
        if os.access("/usr/bin/afplay", os.X_OK):
            self.assertEqual(states["audio"], "ready")
        self.assertEqual(states["node"], "unknown")
        self.assertEqual(set(states), {"python", "bash", "node", "audio", "code", "claude", "codex"})
        self.assertNotIn("xcode-select", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_git_bash_delegates_to_powershell_with_check_flag(self):
        self.executable("uname", "echo MINGW64_NT-10.0")
        self.executable("cygpath", 'printf "%s\\n" "$2"')
        self.executable("powershell.exe", 'printf "%s\\n" "$@" > ' + shlex.quote(str(self.trace)))
        for arguments in ((), ("--check",), ("--global",)):
            with self.subTest(arguments=arguments):
                result = self.run_installer(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                passed = self.trace.read_text().splitlines()
                self.assertIn(str(self.claude / "install.ps1"), passed)
                self.assertEqual("-Check" in passed, arguments == ("--check",))
                self.assertEqual("-Global" in passed, arguments == ("--global",))


if __name__ == "__main__":
    unittest.main()
