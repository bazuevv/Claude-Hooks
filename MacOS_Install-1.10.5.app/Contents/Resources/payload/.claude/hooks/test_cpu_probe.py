import json
import ctypes
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import cpu_probe as probe


class CpuProbeTests(unittest.TestCase):
    def test_turn_lifecycle_and_stop_grace(self):
        start = probe.transition(None, 'abc', 'start', 100, {})
        self.assertEqual(start['expires_at'], 7300)
        stop = probe.transition(start, 'abc', 'stop', 200, {})
        self.assertEqual(stop['expires_at'], 380)
        repeated = probe.transition(stop, 'abc', 'stop', 220, {})
        self.assertEqual(repeated['expires_at'], 380)
        end = probe.transition(repeated, 'abc', 'ui_stop', 250, {})
        self.assertEqual(end['expires_at'], 250)
        self.assertIsNone(probe.transition(end, 'abc', 'stop', 300, {}))
        next_turn = probe.transition(start, 'abc', 'start', 400, {})
        self.assertNotEqual(start['run_id'], next_turn['run_id'])

    def test_disabled_and_stale_ui_stop(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(probe, 'STATE', Path(temp)), \
                patch.object(probe, 'config', return_value={'cpuProbeEnabled':False}), \
                patch.object(probe.subprocess, 'Popen') as spawn:
            probe.note_event('abc.jsonl', 'start')
            self.assertEqual(list(Path(temp).iterdir()), [])
            spawn.assert_not_called()
        with tempfile.TemporaryDirectory() as temp, patch.object(probe, 'STATE', Path(temp)), \
                patch.object(probe, 'config', return_value={'cpuProbeEnabled':True}), \
                patch.object(probe.subprocess, 'Popen') as spawn, patch.object(probe.time, 'time', return_value=200):
            probe.note_event('abc.jsonl', 'start')
            before = (Path(temp) / 'abc.json').read_text()
            probe.note_event('abc.jsonl', 'ui_stop', '1970-01-01T00:01:40Z')
            self.assertEqual((Path(temp) / 'abc.json').read_text(), before)
            self.assertEqual(len(probe.active_controls(201)), 1)
            self.assertEqual(len(probe.active_controls(10000)), 0)
            spawn.assert_called_once()

    def test_machine_normalization_and_pid_reuse(self):
        proc = SimpleNamespace(pid=12, info={'pid':12, 'name':'Code.exe', 'create_time':1})
        usage = [0.0]
        proc.cpu_times = lambda:SimpleNamespace(user=usage[0], system=0)
        proc.threads = lambda:[SimpleNamespace(id=20, user_time=usage[0], system_time=0)]
        proc.cmdline = lambda:['Code.exe', '--type=renderer', '--token=secret']
        fake = SimpleNamespace(Error=RuntimeError, cpu_percent=lambda **kw:[5]*20, process_iter=lambda attrs:[proc])
        clock = [0.0]
        with patch.dict('sys.modules', psutil=fake), patch.object(probe.time, 'monotonic', side_effect=lambda:clock[0]):
            sampler = probe.Sampler(native=False)
            first = sampler.sample()
            self.assertIsNone(first['processes'][0]['cpu_percent_one_core'])
            usage[0], clock[0] = 2, 2
            data = sampler.sample()
            self.assertEqual(data['vscode_cpu_percent_one_core'], 100)
            self.assertEqual(data['vscode_cpu_percent_machine'], 5)
            self.assertEqual(data['busiest_threads'][0]['cpu_percent_one_core'], 100)
            self.assertEqual(data['processes'][0]['role'], 'renderer')
            self.assertNotIn('secret', json.dumps(data))
            proc.info['create_time'] = 10
            usage[0], clock[0] = 1, 4
            data = sampler.sample()
            self.assertIsNone(data['processes'][0]['cpu_percent_one_core'])

    def test_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'capture.jsonl'
            with path.open('wb') as stream:
                stream.truncate(8 * 1024 * 1024)
            probe.append_record(path, {'kind':'check'})
            self.assertEqual(json.loads(path.read_text()), {'kind':'check'})
            self.assertTrue(path.with_suffix('.jsonl.1').exists())

    def test_windows_snapshot_bounds_and_threads(self):
        from cpu_probe_windows import ProcessInfo, ThreadInfo, parse
        psize, tsize = ctypes.sizeof(ProcessInfo), ctypes.sizeof(ThreadInfo)
        name = 'Code.exe'.encode('utf-16-le')
        raw = ctypes.create_string_buffer(psize + tsize + len(name))
        proc = ProcessInfo.from_buffer(raw)
        proc.pid, proc.thread_count, proc.created = 123, 1, 42
        proc.user, proc.kernel = 20000000, 10000000
        proc.name.length = len(name)
        proc.name.buffer = ctypes.addressof(raw) + psize + tsize
        ctypes.memmove(proc.name.buffer, name, len(name))
        thread = ThreadInfo.from_buffer(raw, psize)
        thread.pid, thread.tid, thread.created, thread.user = 123, 456, 9, 10000000
        row = parse(raw, len(raw))[0]
        self.assertEqual(row['total'], 3)
        self.assertEqual(row['threads'], {(456, 9):1})
        thread.pid = 124
        with self.assertRaises(ValueError):
            parse(raw, len(raw))
        thread.pid = 123
        proc.name.buffer = 1
        with self.assertRaises(ValueError):
            parse(raw, len(raw))
        with self.assertRaises(ValueError):
            parse(raw, 10)


if __name__ == '__main__':
    unittest.main()
