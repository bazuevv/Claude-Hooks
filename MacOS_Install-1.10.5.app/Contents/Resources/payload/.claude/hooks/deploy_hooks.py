"""Install hooks for one project or all user projects, preserving existing data."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
from settings_hooks import TEMPLATE_NAME, merge_settings as combine_settings, read_settings, with_shared_settings

# Explicit distribution contents. Only the explicitly supplied API profile may
# be bundled; never collect other accounts, sessions or caches from the host.
DEFAULT_ACCOUNT = "settings_API.json"
SOURCE_TEMPLATE_NAME = "settings-hooks-template.json"
PAYLOAD_NAMES = (
    "hooks", "patches", "settings-hooks.json", "Readme.md", "notification.mp3",
    "message-costs.toml", "install.sh", "install.ps1", ".gitignore", ".hooks-version",
)
PRESERVE = {"patches/claude-custom-config.toml", "message-costs.toml", DEFAULT_ACCOUNT}


def ignored(directory, names):
    return [name for name in names if name == "__pycache__" or name == ".DS_Store"
            or name.endswith((".pyc", ".log", ".original", ".bak"))]


def source_template(source):
    source = Path(source)
    protected = source / SOURCE_TEMPLATE_NAME
    return protected if protected.is_file() else source / TEMPLATE_NAME


def copy_payload(source, destination, *, global_install=False):
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True)
    for name in PAYLOAD_NAMES:
        item = source_template(source) if name == TEMPLATE_NAME else source / name
        if item.is_symlink():
            raise ValueError("Payload contains a symlink: " + str(item))
        if item.is_dir():
            if any(path.is_symlink() for path in item.rglob("*")):
                raise ValueError("Payload directory contains symlinks: " + str(item))
            shutil.copytree(item, destination / name, ignore=ignored)
        else:
            shutil.copy2(item, destination / name)
    # The active global supervisor may remove duplicate registrations from a
    # project's settings-hooks.json. Keep hook registrations in an immutable
    # distribution template and combine only the project's permission policy.
    protected_template = source / SOURCE_TEMPLATE_NAME
    project_template = source / TEMPLATE_NAME
    if protected_template.is_file() and project_template.is_file():
        config = read_settings(destination / TEMPLATE_NAME)
        project_config = read_settings(project_template)
        if isinstance(project_config.get("permissions"), dict):
            config["permissions"] = project_config["permissions"]
        (destination / TEMPLATE_NAME).write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    profile = source / DEFAULT_ACCOUNT
    if profile.is_symlink():
        raise ValueError("Bundled API profile must not be a symlink")
    if profile.exists():
        read_settings(profile)  # Validate without printing credentials.
        shutil.copy2(profile, destination / DEFAULT_ACCOUNT)
        (destination / DEFAULT_ACCOUNT).chmod(0o600)
    config = read_settings(destination / TEMPLATE_NAME)
    old_root, new_root = (("$CLAUDE_PROJECT_DIR", "$HOME") if global_install
                          else ("$HOME", "$CLAUDE_PROJECT_DIR"))
    for groups in config.get("hooks", {}).values():
        for group in groups:
            for handler in group["hooks"]:
                if isinstance(handler.get("command"), str):
                    handler["command"] = handler["command"].replace(
                        old_root + "/.claude/", new_root + "/.claude/")
    (destination / TEMPLATE_NAME).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (destination / "settings.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def merge_settings(existing, incoming):
    if isinstance(existing, dict) and isinstance(incoming, dict):
        result = dict(existing)
        for key, value in incoming.items():
            result[key] = merge_settings(result[key], value) if key in result else value
        return result
    if isinstance(existing, list) and isinstance(incoming, list):
        return existing + [value for value in incoming if value not in existing]
    # Keep existing project preferences; hook registrations and permissions are lists.
    return existing


def overlay(source, destination, prefix=""):
    for item in source.iterdir():
        relative = prefix + item.name
        target = destination / item.name
        if item.is_symlink() or target.is_symlink():
            raise ValueError("Refusing to overwrite a symlink: " + relative)
        if item.is_dir():
            target.mkdir(exist_ok=True)
            overlay(item, target, relative + "/")
        elif target.exists() and relative == "settings.json":
            old = json.loads(target.read_text(encoding="utf-8-sig"))
            new = json.loads(item.read_text(encoding="utf-8-sig"))
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise ValueError("settings.json must contain an object")
            target.write_text(json.dumps(merge_settings(old, new), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif target.exists() and relative == ".gitignore":
            lines = target.read_text(encoding="utf-8").splitlines()
            lines += [line for line in item.read_text(encoding="utf-8").splitlines() if line not in lines]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif not (target.exists() and relative in PRESERVE):
            shutil.copy2(item, target)


def deploy(source, project):
    source, project = Path(source).resolve(), Path(project).resolve()
    if not project.is_dir() or project.name == ".claude":
        raise ValueError("Choose the project directory, not .claude")
    target = project / ".claude"
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ValueError("The project's .claude must be a regular directory")
    if source == target or source in project.parents or target in source.parents:
        raise ValueError("The project must be outside the installer payload")
    backup = None
    with tempfile.TemporaryDirectory(prefix=".claude-install-", dir=project) as work:
        stage = Path(work) / ".claude"
        if target.exists():
            shutil.copytree(target, stage, symlinks=True)
        else:
            stage.mkdir()
        # Validate the incoming registration before touching the live directory.
        config = read_settings(source_template(source))
        if not isinstance(config, dict) or not isinstance(config.get("hooks"), dict):
            raise ValueError("Payload has no hook registrations")
        with tempfile.TemporaryDirectory(prefix="claude-payload-") as clean:
            payload = Path(clean) / ".claude"
            copy_payload(source, payload)
            # Dependency paths come from this installation, not from the app bundle.
            state = source / "install-state"
            if state.is_dir() and not state.is_symlink():
                shutil.copytree(state, payload / "install-state")
            overlay(payload, stage)
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            backup = project / (".claude.backup-" + stamp)
            os.rename(target, backup)
        try:
            os.rename(stage, target)
        except BaseException:
            if backup is not None:
                os.rename(backup, target)
            raise
    return {"project": str(project), "installed": str(target), "backup": str(backup) if backup else None}


def stage_default_account(target, stage, active):
    """Seed the requested profile and never leave an unconfigured active file.

    Return additional transaction entries, never write to the live directory.
    Existing profiles (including differently cased API names) are preserved.
    A real active provider, OAuth credentials or a valid active marker always
    wins. Merely storing another inactive profile must not leave settings.json
    without connection data.
    """
    incoming = stage / DEFAULT_ACCOUNT
    if not incoming.exists():
        return active, [], False
    profiles = [path for path in target.glob("settings*.json")
                if path.name not in ("settings.json", "settings.local.json", TEMPLATE_NAME)]
    existing = next((path for path in profiles if path.name.lower() == DEFAULT_ACCOUNT.lower()), None)
    if existing and existing.is_symlink():
        raise ValueError("Existing API profile must not be a symlink")
    names = [] if existing else [DEFAULT_ACCOUNT]
    account_name = existing.name if existing else DEFAULT_ACCOUNT
    account = read_settings(existing or incoming)
    env = active.get("env", {})
    configured = isinstance(env, dict) and any(env.get(key) for key in (
        "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))
    marker = target / ".active-account"
    marker_name = ""
    if marker.is_file() and not marker.is_symlink():
        marker_name = marker.read_text(encoding="utf-8").strip()
    marker_valid = (Path(marker_name).name == marker_name
                    and marker_name.startswith("settings_") and marker_name.endswith(".json")
                    and (target / marker_name).is_file()
                    and not (target / marker_name).is_symlink())
    configured = configured or marker_valid or (target / ".credentials.json").exists()
    if configured:
        return active, names, False
    # Preserve ordinary user preferences, then apply this profile's connection
    # settings. Shared hooks are merged by the caller immediately afterwards.
    activated = combine_settings(active, account)
    (stage / "settings.json.bak").write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (stage / "settings.json.bak").chmod(0o600)
    (stage / ".active-account").write_text(account_name, encoding="utf-8")
    (stage / ".active-account").chmod(0o600)
    return activated, names + ["settings.json.bak", ".active-account"], True


def deploy_global(source, home=None):
    """Install only managed files; never copy/replace the user's sessions or accounts.

    Stage and validate first. Back up every replaced entry and restore it if a
    commit fails. The active settings are committed last, after scripts exist.
    """
    source = Path(source).resolve()
    target = (Path(home) if home is not None else Path.home()) / ".claude"
    if target.is_symlink():
        raise ValueError("The user's .claude must be a regular directory")
    target.mkdir(parents=True, exist_ok=True)
    config = read_settings(source_template(source))
    if not isinstance(config.get("hooks"), dict):
        raise ValueError("Payload has no hook registrations")
    with tempfile.TemporaryDirectory(prefix=".hooks-install-", dir=target.parent) as work:
        stage = Path(work) / ".claude"
        copy_payload(source, stage, global_install=True)
        # Keep user customizations and extra scripts when updating an installation.
        for name in PAYLOAD_NAMES:
            previous, incoming = target / name, stage / name
            if previous.is_symlink():
                raise ValueError("Refusing to overwrite a symlink: " + name)
            if previous.is_dir():
                merged = Path(work) / (name + "-merged")
                shutil.copytree(previous, merged, symlinks=True)
                overlay(incoming, merged, name + "/")
                shutil.rmtree(incoming)
                os.rename(merged, incoming)
            elif previous.exists() and name in PRESERVE:
                shutil.copy2(previous, incoming)
            elif previous.exists() and name == ".gitignore":
                lines = previous.read_text(encoding="utf-8").splitlines()
                lines += [line for line in incoming.read_text(encoding="utf-8").splitlines() if line not in lines]
                incoming.write_text("\n".join(lines) + "\n", encoding="utf-8")
        old_template = target / TEMPLATE_NAME
        if old_template.exists():
            incoming = read_settings(stage / TEMPLATE_NAME)
            shared = combine_settings(incoming, read_settings(old_template))
            (stage / TEMPLATE_NAME).write_text(json.dumps(shared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        settings = target / "settings.json"
        if settings.is_symlink():
            raise ValueError("Refusing to overwrite settings.json symlink")
        active = read_settings(settings) if settings.exists() else {}
        active, account_entries, activated_account = stage_default_account(target, stage, active)
        active = with_shared_settings(stage, active)
        (stage / "settings.json").write_text(json.dumps(active, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "settings.json").chmod(settings.stat().st_mode & 0o777 if settings.exists() and not activated_account else 0o600)
        names = list(PAYLOAD_NAMES)
        names.extend(account_entries)
        for name in ("install-state",):
            state = source / name
            if state.is_dir() and not state.is_symlink():
                shutil.copytree(state, stage / name)
                names.append(name)
        names.append("settings.json")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        backup = target / "backups" / ("global-hooks-" + stamp)
        backup.mkdir(parents=True)
        committed = []
        try:
            for name in names:
                previous, incoming = target / name, stage / name
                if previous.is_symlink():
                    raise ValueError("Refusing to overwrite a symlink: " + name)
                existed = previous.exists()
                if existed:
                    if previous.is_dir():
                        shutil.copytree(previous, backup / name, symlinks=True)
                    else:
                        shutil.copy2(previous, backup / name)
                # Directories require an intermediate move; regular files use replace.
                if previous.is_dir():
                    os.rename(previous, Path(work) / (name + "-old"))
                committed.append((name, existed))
                os.replace(incoming, previous)
        except BaseException:
            for name, existed in reversed(committed):
                previous = target / name
                if previous.is_dir():
                    shutil.rmtree(previous)
                elif previous.exists():
                    previous.unlink()
                if existed:
                    os.replace(backup / name, previous)
            raise
    return {"installed": str(target), "backup": str(backup), "scope": "user",
            "activated_account": DEFAULT_ACCOUNT if activated_account else None}


def initialize_global(target):
    """Activate extension patches and server only for explicit installer actions."""
    subprocess.run([sys.executable, "-B", str(Path(target) / "hooks/hook_supervisor.py"), "--initialize"],
                   check=True, timeout=300, stdin=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--project")
    scope.add_argument("--global", dest="global_install", action="store_true")
    options = parser.parse_args()
    try:
        result = deploy_global(options.source) if options.global_install else deploy(options.source, options.project)
        if options.global_install:
            initialize_global(result["installed"])
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + "\n")
