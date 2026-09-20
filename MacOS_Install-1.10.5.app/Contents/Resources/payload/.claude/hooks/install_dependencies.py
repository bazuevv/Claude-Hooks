"""Shared dependency installer. Entry points: ../install.sh and ../install.ps1.

Python 3.9 can import this module for tests; running hooks requires 3.11+.
No hook is imported during the dependency probe (no accounts or services touched).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

CLAUDE = Path(__file__).resolve().parent.parent
PYTHON_MODULES = (
    "tomllib", "ssl", "sqlite3", "ctypes", "http.server", "urllib.request",
    "json", "hashlib", "threading", "subprocess", "dataclasses", "pathlib",
)
PACKAGES = {
    "brew": {"node": "node", "ffplay": "ffmpeg", "code": "visual-studio-code"},
    "winget": {"bash": "Git.Git", "node": "OpenJS.NodeJS.LTS",
               "ffplay": "Gyan.FFmpeg", "code": "Microsoft.VisualStudioCode"},
    "apt-get": {"bash": "bash", "node": "nodejs", "ffplay": "ffmpeg", "wpctl": "wireplumber"},
    "dnf": {"bash": "bash", "node": "nodejs", "ffplay": "ffmpeg-free", "wpctl": "wireplumber"},
    "pacman": {"bash": "bash", "node": "nodejs", "ffplay": "ffmpeg", "wpctl": "wireplumber"},
    "zypper": {"bash": "bash", "node": "nodejs22", "ffplay": "ffmpeg", "wpctl": "wireplumber"},
}


def emit(dependency, state, path=None, *, stage=None, percent=None):
    """Stable, language-independent events; ordinary CLI output stays intact."""
    if os.environ.get("CLAUDE_INSTALL_EVENTS") == "1":
        event = {"id": dependency, "state": state}
        if path:
            event["path"] = str(path)
        if stage:
            event["stage"] = stage
        if percent is not None:
            event["percent"] = max(0, min(100, percent))
        print("@claude-installer " + json.dumps(event, ensure_ascii=True), flush=True)


def check_python():
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required; run .claude/install.sh")
    for module in PYTHON_MODULES + (("msvcrt",) if os.name == "nt" else ("fcntl",)):
        importlib.import_module(module)
    # Check compiled modules, not just their Python wrappers.
    import ssl
    import sqlite3
    ssl.create_default_context()
    with sqlite3.connect(":memory:") as connection:
        connection.execute("select 1")


def execute(command, *, capture=False, timeout=1800):
    print("+ " + subprocess.list2cmdline([str(arg) for arg in command]), flush=True)
    return subprocess.run(command, check=True, text=True, encoding="utf-8",
                          errors="replace", capture_output=capture, timeout=timeout)


def download(url, destination, dependency=None):
    print("Download: " + url, flush=True)
    with urllib.request.urlopen(url, timeout=90) as response, open(destination, "wb") as output:
        total = int(response.headers.get("Content-Length", "0"))
        received, previous = 0, -1
        if dependency:
            emit(dependency, "installing", stage="downloading", percent=0 if total else None)
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            percent = min(99, int(received * 100 / total)) if total else None
            if dependency and percent != previous:
                emit(dependency, "installing", stage="downloading", percent=percent)
                previous = percent
        if total and received != total:
            raise RuntimeError("Incomplete download: " + url)
        if dependency:
            emit(dependency, "installing", stage="downloading", percent=100)


def extension_package_url(identifier, target):
    # Marketplace's package endpoint requires a concrete version (not "latest").
    # Ask the gallery for assets so platform-specific packages keep their names.
    body = {"filters": [{"criteria": [{"filterType": 7, "value": identifier}],
                          "pageNumber": 1, "pageSize": 1}], "flags": 19}
    request = urllib.request.Request(
        "https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
            "Accept": "application/json;api-version=7.2-preview.1"})
    with urllib.request.urlopen(request, timeout=90) as response:
        catalog = json.load(response)
    for result in catalog.get("results", []):
        for extension in result.get("extensions", []):
            for version in extension.get("versions", []):
                properties = {p["key"]: p["value"] for p in version.get("properties", [])}
                if properties.get("Microsoft.VisualStudio.Code.PreRelease", "false").lower() == "true":
                    continue
                if version.get("targetPlatform", "universal") not in (target, "universal", "undefined"):
                    continue
                for asset in version.get("files", []):
                    if asset.get("assetType") == "Microsoft.VisualStudio.Services.VSIXPackage":
                        url = asset.get("source", "")
                        if url.startswith("https://"):
                            return url
    raise RuntimeError("Marketplace has no compatible package: " + identifier + " / " + target)


def vscode_target(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    architecture = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}.get(system, system.lower()) + "-" + architecture


def installed_extensions_from_disk(home=None, system=None, machine=None):
    """Read desktop VS Code's preserved extension store without launching Code."""
    home = Path(home) if home is not None else Path.home()
    expected_target = vscode_target(system, machine)
    installed = set()
    for root in (home / ".vscode/extensions", home / ".vscode-insiders/extensions"):
        obsolete = set()
        try:
            data = json.loads((root / ".obsolete").read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                obsolete = {str(name).lower() for name, removed in data.items() if removed}
        except (OSError, ValueError, UnicodeError):
            pass
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for directory in entries:
            if not directory.is_dir() or directory.name.lower() in obsolete:
                continue
            try:
                manifest = json.loads((directory / "package.json").read_text(encoding="utf-8-sig"))
                identifier = (str(manifest["publisher"]) + "." + str(manifest["name"])).lower()
                metadata = manifest.get("__metadata") if isinstance(manifest.get("__metadata"), dict) else {}
                target = str(metadata.get("targetPlatform", "universal")).lower()
                entry = manifest.get("main") or manifest.get("browser")
                if target not in ("universal", "undefined", expected_target):
                    continue
                if not isinstance(entry, str) or not (directory / entry).is_file():
                    continue
                installed.add(identifier)
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                continue
    return installed


def existing_macos_vscode(home=None, applications=None):
    """Return an existing stable VS Code bundle without launching it."""
    home = Path(home) if home is not None else Path.home()
    applications = Path(applications) if applications is not None else Path("/Applications")
    candidates = (home / "Applications/Visual Studio Code.app",
                  applications / "Visual Studio Code.app")
    return next((candidate for candidate in candidates if candidate.exists()), None)


class Installer:
    def __init__(self, check=False, system=None, claude=CLAUDE):
        self.check = check
        self.system = system or platform.system()
        self.claude = Path(claude)
        self.paths = []
        self.errors = []
        self.apt_updated = False
        self.manager = None
        self.refresh_paths()

    def refresh_paths(self):
        directories = []
        if self.system == "Darwin":
            directories += ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin",
                            str(Path.home() / "Library/Application Support/ClaudeHooks/tools/node/bin"),
                            "/Applications/Visual Studio Code.app/Contents/Resources/app/bin",
                            str(Path.home() / "Applications/Visual Studio Code.app/Contents/Resources/app/bin")]
        elif self.system == "Windows":
            local = os.environ.get("LOCALAPPDATA", "")
            program_files = os.environ.get("ProgramFiles", "C:/Program Files")
            directories += [os.path.join(program_files, "Git", "bin"),
                            os.path.join(program_files, "nodejs"),
                            os.path.join(program_files, "Microsoft VS Code", "bin"),
                            os.path.join(local, "Programs", "Git", "bin"),
                            os.path.join(local, "Programs", "Microsoft VS Code", "bin"),
                            os.path.join(local, "Microsoft", "WindowsApps"),
                            os.path.join(local, "Microsoft", "WinGet", "Links")]
            directories += [os.path.dirname(path) for path in glob.glob(
                os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "ffplay.exe"),
                recursive=True)]
        directories += [str(Path.home() / ".local/bin")]
        for directory in directories:
            if os.path.isdir(directory) and directory not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")

    def remember(self, executable):
        directory = os.path.dirname(os.path.abspath(executable))
        if directory not in self.paths:
            self.paths.append(directory)

    def probe(self, name):
        arguments = {
            "bash": ["-c", 'test -n "$BASH_VERSION"'],
            "node": ["-e", 'process.exit(Number(process.versions.node.split(".")[0]) >= 18 ? 0 : 1)'],
            "ffplay": ["-version"], "wpctl": ["--version"], "code": ["--version"],
        }
        if name == "bash" and self.system == "Windows":
            arguments[name] = ["-c", 'case "$(uname -s)" in MINGW*|MSYS*) exit 0;; *) exit 1;; esac']
        checked = set()
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            executable = shutil.which(name, path=directory)
            if not executable or executable in checked:
                continue
            checked.add(executable)
            try:
                subprocess.run([executable, *arguments[name]], check=True, capture_output=True,
                               timeout=30)
            except (OSError, subprocess.SubprocessError):
                continue
            self.remember(executable)
            return executable
        return None

    def privileged(self, command):
        if os.geteuid() == 0:
            return execute(command)
        sudo = shutil.which("sudo")
        if not sudo:
            raise RuntimeError("sudo is required to install system packages")
        return execute([sudo, *command])

    def package_manager(self):
        if self.manager:
            return self.manager
        if self.system == "Windows":
            if not shutil.which("winget"):
                execute(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(self.claude / "install.ps1"), "-BootstrapWinGet"])
                self.refresh_paths()
            if not shutil.which("winget"):
                raise RuntimeError("WinGet installation failed; restart the terminal and rerun install.ps1")
            self.manager = "winget"
        elif self.system == "Darwin":
            if not shutil.which("brew"):
                with tempfile.TemporaryDirectory(prefix="claude-brew-") as directory:
                    script = Path(directory) / "install.sh"
                    download("https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh", script)
                    execute(["/bin/bash", str(script)])
                self.refresh_paths()
            if not shutil.which("brew"):
                raise RuntimeError("Homebrew installation failed")
            self.manager = "brew"
        elif self.system == "Linux":
            self.manager = next((name for name in ("apt-get", "dnf", "pacman", "zypper")
                                 if shutil.which(name)), None)
            if not self.manager:
                raise RuntimeError("Supported Linux package managers: apt-get, dnf, pacman, zypper")
        else:
            raise RuntimeError("Unsupported OS: " + self.system)
        return self.manager

    def install_code_linux(self, manager):
        arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
            platform.machine().lower())
        if not arch:
            raise RuntimeError("VS Code auto-install supports x64 and arm64")
        if manager not in ("apt-get", "dnf", "zypper"):
            raise RuntimeError("Install official VS Code on this distribution, then rerun install.sh")
        suffix = "deb" if manager == "apt-get" else "rpm"
        with tempfile.TemporaryDirectory(prefix="claude-code-") as directory:
            package = Path(directory) / ("code." + suffix)
            download(f"https://update.code.visualstudio.com/latest/linux-{suffix}-{arch}/stable", package)
            if manager == "apt-get":
                self.privileged([manager, "install", "-y", str(package)])
            elif manager == "dnf":
                self.privileged([manager, "install", "-y", str(package)])
            else:
                self.privileged([manager, "--non-interactive", "install", str(package)])

    def install_package(self, dependency):
        if self.check:
            raise RuntimeError("Installation is disabled in --check mode")
        if self.system == "Darwin" and dependency in ("node", "code"):
            self.install_macos_tool(dependency)
            self.refresh_paths()
            return
        manager = self.package_manager()
        if manager == "apt-get" and not self.apt_updated:
            self.privileged([manager, "update"])
            self.apt_updated = True
        if dependency == "code" and self.system == "Linux":
            self.install_code_linux(manager)
        else:
            package = PACKAGES[manager][dependency]
            if manager == "winget":
                execute(["winget", "install", "--id", package, "--exact", "--source", "winget",
                         "--accept-package-agreements", "--accept-source-agreements"])
            elif manager == "brew":
                # Upgrade only this dependency when an installed version failed its probe.
                installed = subprocess.run(["brew", "list", "--versions", package],
                                           capture_output=True, timeout=30)
                action = "upgrade" if installed.returncode == 0 and installed.stdout.strip() else "install"
                execute(["brew", action, *(["--cask"] if dependency == "code" else []), package])
            elif manager in ("apt-get", "dnf"):
                self.privileged([manager, "install", "-y", package])
            elif manager == "pacman":
                self.privileged([manager, "-S", "--needed", "--noconfirm", package])
            else:
                self.privileged([manager, "--non-interactive", "install", package])
        self.refresh_paths()

    def install_macos_tool(self, dependency):
        """User-scoped official distributions, usable from Finder without sudo."""
        arch = {"x86_64": "x64", "arm64": "arm64"}.get(platform.machine().lower())
        if not arch:
            raise RuntimeError("Unsupported macOS architecture")
        if dependency == "code":
            existing = existing_macos_vscode()
            if existing is not None:
                raise RuntimeError(
                    f"VS Code already exists at {existing}, but did not pass the executable checks. "
                    "It was not replaced and no second copy was installed. Repair or update it, then retry."
                )
        with tempfile.TemporaryDirectory(prefix="claude-tools-") as directory:
            temporary = Path(directory)
            if dependency == "node":
                base = "https://nodejs.org/dist/latest-v22.x/"
                sums = temporary / "SHASUMS256.txt"
                download(base + sums.name, sums)
                pattern = re.compile(r"([a-f0-9]{64})\s+(node-v22\.\d+\.\d+-darwin-" + arch + r"\.tar\.gz)")
                match = next((pattern.fullmatch(line.strip()) for line in sums.read_text().splitlines()
                              if pattern.fullmatch(line.strip())), None)
                if match is None:
                    raise RuntimeError("Official Node.js checksum manifest has no matching build")
                archive = temporary / match[2]
                download(base + archive.name, archive, dependency)
                emit(dependency, "installing", stage="verifying")
                digest = hashlib.sha256()
                with archive.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != match[1]:
                    raise RuntimeError("Node.js archive checksum mismatch")
                destination = Path.home() / "Library/Application Support/ClaudeHooks/tools/node/bin"
                destination.mkdir(parents=True, exist_ok=True)
                emit(dependency, "installing", stage="extracting")
                # Only the executable is needed; never extract archive paths or symlinks.
                with tarfile.open(archive) as bundle:
                    entry = bundle.getmember(archive.name[:-7] + "/bin/node")
                    if not entry.isfile():
                        raise RuntimeError("Invalid Node.js executable in archive")
                    candidate = temporary / "node"
                    with bundle.extractfile(entry) as source, candidate.open("wb") as target:
                        shutil.copyfileobj(source, target)
                candidate.chmod(0o755)
                emit(dependency, "installing", stage="installing")
                execute([str(candidate), "--version"], capture=True, timeout=30)
                shutil.copy2(candidate, destination / "node.new")
                os.replace(destination / "node.new", destination / "node")
            else:
                flavor = "darwin-arm64" if arch == "arm64" else "darwin"
                archive = temporary / "vscode.zip"
                download(f"https://update.code.visualstudio.com/latest/{flavor}/stable", archive, dependency)
                emit(dependency, "installing", stage="extracting")
                execute(["/usr/bin/ditto", "-x", "-k", str(archive), str(temporary / "unpacked")])
                app = temporary / "unpacked/Visual Studio Code.app"
                if not (app / "Contents/Resources/app/bin/code").is_file():
                    raise RuntimeError("Invalid VS Code archive")
                applications = Path.home() / "Applications"
                applications.mkdir(exist_ok=True)
                destination = applications / app.name
                emit(dependency, "installing", stage="installing")
                shutil.move(str(app), str(destination))

    def ensure(self, name):
        event_id = "audio" if name == "ffplay" else name
        emit(event_id, "checking")
        executable = self.probe(name)
        if executable:
            emit(event_id, "ready", executable)
            print(f"[OK] {name}: {executable}", flush=True)
            return executable
        print(f"[MISSING] {name}", flush=True)
        if self.check:
            emit(event_id, "missing")
            self.errors.append(name)
            return None
        emit(event_id, "installing")
        self.install_package(name)
        emit(event_id, "installing", stage="verifying")
        executable = self.probe(name)
        if not executable:
            raise RuntimeError(f"{name} is still unavailable/incompatible after installation. "
                               "Check package repository versions and rerun install.")
        print(f"[OK] {name}: {executable}", flush=True)
        emit(event_id, "ready", executable)
        return executable

    def ensure_extension(self, code, identifier):
        event_id = "claude" if identifier == "anthropic.claude-code" else "codex"
        emit(event_id, "checking")
        result = execute([code, "--list-extensions"], capture=True, timeout=60)
        if identifier.lower() in {line.strip().lower() for line in result.stdout.splitlines()}:
            print(f"[OK] extension {identifier}", flush=True)
            if event_id == "claude":
                emit(event_id, "ready")
            return True
        print(f"[MISSING] extension {identifier}", flush=True)
        if self.check:
            emit(event_id, "missing")
            self.errors.append(identifier)
            return False
        emit(event_id, "installing")
        if self.system == "Darwin":
            target = "darwin-arm64" if platform.machine().lower() == "arm64" else "darwin-x64"
            with tempfile.TemporaryDirectory(prefix="claude-extension-") as directory:
                package = Path(directory) / (identifier + ".vsix")
                download(extension_package_url(identifier, target), package, event_id)
                emit(event_id, "installing", stage="installing")
                execute([code, "--install-extension", str(package)], timeout=300)
        else:
            execute([code, "--install-extension", identifier], timeout=300)
        emit(event_id, "installing", stage="verifying")
        installed = execute([code, "--list-extensions"], capture=True, timeout=60)
        if identifier.lower() not in {line.strip().lower() for line in installed.stdout.splitlines()}:
            raise RuntimeError("Extension installation could not be verified: " + identifier)
        if event_id == "claude":
            emit(event_id, "ready")
        return True

    def codex_available(self):
        from codex_app_server import find_codex_binary, CodexAppServerError
        try:
            binary = find_codex_binary()
            subprocess.run([binary, "--version"], check=True, capture_output=True, timeout=30)
        except (CodexAppServerError, OSError, subprocess.SubprocessError):
            return False
        self.remember(binary)
        print("[OK] Codex: " + binary, flush=True)
        emit("codex", "ready", binary)
        return True

    def report(self, include_python=True):
        """Check every dependency, even with Python 3.9. Never install or save state."""
        missing = []
        def result(name, available, path=None):
            emit(name, "ready" if available else "missing", path)
            if not available:
                missing.append(name)
        if include_python:
            emit("python", "checking")
            try:
                check_python()
            except (RuntimeError, ImportError, OSError):
                result("python", False)
            else:
                result("python", True, sys.executable)
        for name in ("bash", "node"):
            emit(name, "checking")
            executable = self.probe(name)
            result(name, bool(executable), executable)
        emit("audio", "checking")
        audio = "/usr/bin/afplay" if self.system == "Darwin" and os.access("/usr/bin/afplay", os.X_OK) else self.probe("ffplay")
        result("audio", bool(audio), audio)
        if self.system == "Linux":
            result("wpctl", bool(self.probe("wpctl")))
        emit("code", "checking")
        code = self.probe("code")
        result("code", bool(code), code)
        emit("claude", "checking")
        installed_extensions = set()
        if code:
            extensions = execute([code, "--list-extensions"], capture=True, timeout=60)
            installed_extensions = {line.strip().lower() for line in extensions.stdout.splitlines()}
        else:
            installed_extensions = installed_extensions_from_disk(system=self.system)
        result("claude", "anthropic.claude-code" in installed_extensions)
        emit("codex", "checking")
        if "openai.chatgpt" not in installed_extensions or not self.codex_available():
            result("codex", False)
        if self.system == "Windows":
            execute(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing"], capture=True, timeout=30)
        return 1 if missing else 0

    def report_code(self):
        """Check only VS Code for the macOS installer's second-stage probe."""
        code = self.probe("code")
        emit("code", "ready" if code else "missing", code)
        return 0 if code else 1

    def save_environment(self):
        state = self.claude / "install-state"
        state.mkdir(exist_ok=True)
        # Plain text paths, never sourced as shell code. Do not commit this directory.
        self.remember(sys.executable)
        for name, content in (("python-path.txt", sys.executable + "\n"),
                              ("bin-paths.txt", "\n".join(self.paths) + "\n")):
            temporary = state / (name + ".tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, state / name)

    def run(self):
        emit("python", "checking")
        check_python()
        emit("python", "ready", sys.executable)
        print(f"[OK] Python {platform.python_version()}: {sys.executable}; standard-library modules", flush=True)
        if not self.check:
            # Populate the whole UI before the first download begins. In
            # particular, VS Code and preserved extensions are known while an
            # earlier missing component (usually Node.js) is still waiting.
            print("Checking all remaining dependencies before installation...", flush=True)
            self.report(include_python=False)
        for name in ("bash", "node"):
            self.ensure(name)
        if self.system == "Darwin" and os.access("/usr/bin/afplay", os.X_OK):
            print("[OK] audio: /usr/bin/afplay", flush=True)
            emit("audio", "ready", "/usr/bin/afplay")
        else:
            self.ensure("ffplay")
        if self.system == "Linux":
            self.ensure("wpctl")
        if self.system == "Windows":
            # The Windows hooks use Windows PowerShell/.NET, not PowerShell Core.
            execute(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                     "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing"],
                    capture=True, timeout=30)
        code = self.ensure("code")
        if code:
            self.ensure_extension(code, "anthropic.claude-code")
        emit("codex", "checking")
        # A standalone CLI cannot replace the required VS Code extension.
        if code and self.ensure_extension(code, "openai.chatgpt"):
            if not self.codex_available():
                emit("codex", "missing" if self.check else "error")
                self.errors.append("Codex CLI")
                if not self.check:
                    raise RuntimeError("Codex extension is present but no working CLI was found. "
                                       "Check CODEX_BIN and reinstall the extension for this OS.")
        elif not code:
            emit("codex", "unknown")
            self.errors.append("openai.chatgpt")
        if self.errors:
            print("Missing dependencies: " + ", ".join(self.errors), file=sys.stderr)
            return 1
        if not self.check:
            self.save_environment()
        print("Dependencies verified. Restart VS Code after installation.", flush=True)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Read-only dependency check")
    parser.add_argument("--probe-python", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", action="store_true", help="Machine-readable read-only dependency report")
    parser.add_argument("--report-code", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--prepare", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--global", dest="global_install", action="store_true",
                        help="Install dependencies and deploy hooks to the user's ~/.claude")
    args = parser.parse_args(argv)
    if args.global_install and (args.check or args.report or args.report_code or args.prepare or args.probe_python):
        parser.error("--global cannot be combined with read-only checks or internal probes")
    if args.report_code and (args.check or args.report or args.prepare or args.probe_python):
        parser.error("--report-code cannot be combined with other modes")
    try:
        if args.probe_python:
            check_python()
            print(sys.executable)
            return 0
        if args.report_code:
            os.environ["CLAUDE_INSTALL_EVENTS"] = "1"
            return Installer(check=True).report_code()
        if args.report or args.prepare:
            os.environ["CLAUDE_INSTALL_EVENTS"] = "1"
            job = Installer(check=True)
            result = job.report()
            if result == 0 and args.prepare and not args.report and not args.check:
                job.save_environment()
            return result
        result = Installer(check=args.check).run()
        if result == 0 and args.global_install:
            from deploy_hooks import deploy_global, initialize_global
            deployed = deploy_global(CLAUDE)
            initialize_global(deployed["installed"])
            print(json.dumps(deployed, ensure_ascii=False))
        return result
    except (OSError, RuntimeError, ValueError, ImportError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print("Installation failed: " + str(exc), file=sys.stderr)
        emit("operation", "error")
        return 2 if args.report or args.prepare else 1


if __name__ == "__main__":
    raise SystemExit(main())
