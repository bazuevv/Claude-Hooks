"""Lifecycle regressions; fixtures only, no live servers/editors/settings."""
import copy
import importlib.util
import sys
import types
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import hook_supervisor as supervisor


class SupervisorTests(unittest.TestCase):
    def test_migrate_exact_enabled_duplicates_only_and_keep_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'global'
            local = Path(directory) / 'проект A/.claude'
            root.mkdir(); local.mkdir(parents=True)
            global_hook = {'type': 'command', 'command': 'bash "$HOME/.claude/hooks/_run.sh" patch-claude-webview.py'}
            group = {'hooks': [global_hook]}
            common = {'hooks': {'SessionStart': [group]}}
            for name in ('settings.json', 'settings-hooks.json'):
                (root / name).write_text(json.dumps(common))
            project_hook = {**global_hook, 'command': global_hook['command'].replace('$HOME', '$CLAUDE_PROJECT_DIR')}
            custom = {'type': 'command', 'command': 'echo custom'}
            extra = {**project_hook, 'command': project_hook['command'] + ' && echo preserve'}
            project = {'env': {'PRIVATE': 'fixture'}, 'permissions': {'allow': ['Read']},
                       'hooks': {'SessionStart': [{'hooks': [project_hook, custom, extra]}],
                                 'Stop': [{'hooks': [project_hook]}]}}
            original = json.dumps(project).encode()
            path = local / 'settings.json'; path.write_bytes(original)
            template = local / 'settings-hooks.json'; template.write_bytes(original)
            changed = supervisor.deduplicate_project(local.parent, root)
            self.assertEqual(changed, [str(path.resolve())])
            expected = copy.deepcopy(project)
            expected['hooks']['SessionStart'][0]['hooks'] = [custom, extra]
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(template.read_bytes(), original)
            self.assertEqual(next((local / 'backups').glob('*/settings.json')).read_bytes(), original)
            self.assertEqual(supervisor.deduplicate_project(local.parent, root), [])
            # Globally disabled hook must not be removed from a project.
            (root / 'settings.json').write_text('{}')
            path.write_bytes(original)
            self.assertEqual(supervisor.deduplicate_project(local.parent, root), [])

    def test_symlink_project_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'project').mkdir(); (root / 'real').mkdir()
            (root / 'real/settings.json').write_text('{"keep":true}')
            (root / 'project/.claude').symlink_to(root / 'real', target_is_directory=True)
            self.assertEqual(supervisor.deduplicate_project(root / 'project', root / 'missing'), [])

    def test_window_freshness_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / 'windows').mkdir()
            wid = 'a' * 32
            file = runtime / 'windows' / (wid + '.json')
            record = {'windowId': wid, 'ts': time.time() * 1000, 'projects': []}
            file.write_text(json.dumps(record))
            self.assertEqual(supervisor.window_registration(wid, runtime), record)
            self.assertIsNone(supervisor.window_registration('../outside', runtime))
            file.write_text(json.dumps({**record, 'ts': (time.time() - 60) * 1000}))
            self.assertIsNone(supervisor.window_registration(wid, runtime))

    def test_server_spawn_is_locked_on_unix_and_windows(self):
        spec = importlib.util.spec_from_file_location('supervisor_lock_test', Path(__file__).with_name('patch-claude-webview.py'))
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(web, '_spawn_lock_path', return_value=directory + '/spawn.lock'):
            first = web._acquire_spawn_lock()
            self.assertIsNotNone(first)
            self.assertIsNone(web._acquire_spawn_lock())
            web._release_spawn_lock(first)
            second = web._acquire_spawn_lock()
            self.assertIsNotNone(second)
            web._release_spawn_lock(second)
            windows = types.SimpleNamespace(LK_NBLCK=1, locking=mock.Mock())
            with mock.patch.dict(sys.modules, {'msvcrt': windows}), mock.patch.object(web.os, 'name', 'nt'):
                lock = web._acquire_spawn_lock()
                self.assertIsNotNone(lock)
                windows.locking.assert_called_once_with(lock.fileno(), 1, 1)
                web._release_spawn_lock(lock)
                windows.locking.side_effect = OSError('busy')
                self.assertIsNone(web._acquire_spawn_lock())

    def test_initialize_uses_same_python_and_verifies_server(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(supervisor, 'ROOT', Path(directory)), \
                mock.patch.object(supervisor.subprocess, 'run') as run, \
                mock.patch.object(supervisor, 'ensure_server') as ensure:
            supervisor.initialize()
            self.assertEqual(len(run.call_args_list), 4)
            self.assertTrue(all(c.args[0][0] == supervisor.sys.executable for c in run.call_args_list))
            ensure.assert_called_once()


if __name__ == '__main__':
    unittest.main()
