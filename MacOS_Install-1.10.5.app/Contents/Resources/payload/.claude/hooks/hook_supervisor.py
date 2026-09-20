"""VS Code lifecycle: one global server and removal of duplicate registrations.

Called by the extension host on activation / workspace changes / every 30s.
Only this bundle's exact registered commands are migrated, with backups.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from hook_paths import ROOT, GLOBAL_INSTALL


def canonical_command(command):
    if not isinstance(command, str):
        return command
    # Match known installation locations only, never arbitrary commands ending
    # in a familiar filename. Extra shell commands must remain untouched.
    for prefix in ('$HOME/.claude/', '${HOME}/.claude/', '$CLAUDE_PROJECT_DIR/.claude/',
                   '${CLAUDE_PROJECT_DIR}/.claude/', '.claude/'):
        command = command.replace('"' + prefix, '"@claude/')
    return command


def registration_key(event, group, hook):
    normalized = dict(hook)
    normalized['command'] = canonical_command(normalized.get('command'))
    context = {k: v for k, v in group.items() if k != 'hooks'}
    return json.dumps([event, context, normalized], sort_keys=True)


def deduplicate_project(project, root=ROOT):
    project, root = Path(project).resolve(), Path(root).resolve()
    local = project / '.claude'
    if local == root or local.is_symlink() or not local.is_dir():
        return []
    # Only remove hooks actually enabled globally, not merely in a template.
    active = json.loads((root / 'settings.json').read_text(encoding='utf-8-sig'))
    known = set()
    template = json.loads((root / 'settings-hooks.json').read_text(encoding='utf-8-sig'))
    for event, groups in template.get('hooks', {}).items():
        for group in groups:
            for hook in group.get('hooks', []):
                if hook.get('type') == 'command' and '/hooks/_run.sh' in hook.get('command', ''):
                    known.add(registration_key(event, group, hook))
    enabled = {registration_key(event, group, hook)
               for event, groups in active.get('hooks', {}).items()
               for group in groups for hook in group.get('hooks', [])} & known
    changed = []
    # settings-hooks.json is the distributable template, not an active Claude
    # settings file. Rewriting it would corrupt future installs and updates.
    for name in ('settings.json', 'settings.local.json'):
        path = local / name
        if path.is_symlink() or not path.is_file():
            continue
        original = path.read_bytes()
        try:
            data = json.loads(original.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            continue  # JSONC / invalid JSON belongs to the user; do not rewrite.
        events = data.get('hooks')
        if not isinstance(events, dict):
            continue
        removed = False
        for event, groups in list(events.items()):
            if not isinstance(groups, list):
                continue
            remaining = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                    remaining.append(group); continue
                hooks = [h for h in group['hooks'] if not isinstance(h, dict) or
                         registration_key(event, group, h) not in enabled]
                if len(hooks) == len(group['hooks']):
                    remaining.append(group)
                else:
                    removed = True
                    if hooks:
                        remaining.append({**group, 'hooks': hooks})
            if remaining:
                events[event] = remaining
            else:
                events.pop(event, None)
        if not removed:
            continue
        if not events:
            data.pop('hooks', None)
        if (local / 'backups').is_symlink():
            raise ValueError('Refusing redirected project backups: ' + str(local))
        backup = local / 'backups' / ('global-migration-' + uuid.uuid4().hex)
        backup.mkdir(parents=True)
        shutil.copy2(path, backup / name)
        fd, temporary = tempfile.mkstemp(prefix='.' + name, dir=local)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
            os.chmod(temporary, path.stat().st_mode & 0o777)
            if path.read_bytes() != original or path.is_symlink():
                raise RuntimeError('Project settings changed during migration: ' + str(path))
            os.replace(temporary, path)
            changed.append(str(path))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return changed


def window_registration(window_id, runtime, max_age=15):
    if not isinstance(window_id, str) or len(window_id) != 32 or any(c not in '0123456789abcdef' for c in window_id):
        return None
    try:
        data = json.loads((Path(runtime) / 'windows' / (window_id + '.json')).read_text())
        age = time.time() - float(data['ts']) / 1000
        if data.get('windowId') == window_id and 0 <= age <= max_age and isinstance(data.get('projects'), list) and all(isinstance(p, str) for p in data['projects']):
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def ensure_server():
    spec = importlib.util.spec_from_file_location('supervised_webview', ROOT / 'hooks/patch-claude-webview.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    problems = module._ensure_http_server()
    if problems:
        raise RuntimeError('; '.join(problems))


def initialize():
    state = ROOT / 'install-state'
    state.mkdir(exist_ok=True)
    # Always record the validated interpreter, including direct CLI deployments.
    (state / 'python-path.txt').write_text(sys.executable + '\n', encoding='utf-8')
    for name in ('patch-extension-csp.py', 'patch-extension-settings.py', 'localize.py', 'patch-claude-webview.py'):
        subprocess.run([sys.executable, '-B', str(ROOT / 'hooks' / name)],
                       input='{"hook_event_name":"SessionStart"}', text=True, check=True,
                       timeout=120, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    ensure_server()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--project', action='append', default=[])
    options = parser.parse_args()
    if not GLOBAL_INSTALL:
        return 0
    os.environ.pop('CLAUDE_PROJECT_DIR', None)
    failures = []
    for project in options.project:
        try:
            deduplicate_project(project)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            failures.append(str(exc))
    if options.initialize:
        initialize()
    else:
        ensure_server()
    for failure in failures:
        print(failure, file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
