"""Independent, bounded CPU capture for chat turns. No prompts or command lines logged."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / '.claude/patches/claude-custom-config.toml'
STATE = ROOT / '.claude/hooks-runtime/cpu-probe-state'
LOG = ROOT / '.claude/hooks-runtime/cpu-usage.jsonl'


def config():
    try:
        with CONFIG.open('rb') as stream:
            return tomllib.load(stream)
    except (OSError, ValueError):
        return {}


def number(cfg, key, default, minimum, maximum):
    value = cfg.get(key, default)
    return max(minimum, min(maximum, value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def append_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= 8 * 1024 * 1024:
        os.replace(path, path.with_suffix(path.suffix + '.1'))
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def transition(previous, session, event, now, cfg):
    if event == 'start':
        return dict(session=session, run_id=uuid.uuid4().hex, started_at=now,
                    phase='running', event_at=now,
                    expires_at=now + number(cfg, 'cpuProbeMaxMinutes', 120, 1, 1440) * 60)
    if not previous or previous.get('expires_at', 0) < now:
        return None
    result = dict(previous, event_at=now)
    if event == 'stop' and previous.get('phase') == 'running':
        result.update(phase='after_server_stop', expires_at=min(previous['expires_at'], now + 180))
    elif event == 'ui_stop':
        result.update(phase='ui_stopped', expires_at=now)
    return result


def note_event(transcript, event, completed_at=None):
    """Called from hooks/server. Failure must never block the chat."""
    try:
        cfg = config()
        if cfg.get('cpuProbeEnabled') is not True:
            return
        session = Path(transcript).stem
        # Only a transcript identifier is persisted, never its path or contents.
        if not session or len(session) > 100 or any(c not in '0123456789abcdefABCDEF-' for c in session):
            return
        STATE.mkdir(parents=True, exist_ok=True)
        target = STATE / (session + '.json')
        try:
            previous = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = None
        if completed_at and previous:
            from datetime import datetime
            if datetime.fromisoformat(completed_at.replace('Z', '+00:00')).timestamp() < previous['started_at']:
                return  # A delayed UI event for the preceding turn.
        value = transition(previous, session, event, time.time(), cfg)
        if value is None:
            return
        temp = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temp, target)
        if event == 'start':
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--worker'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except Exception as exc:
        try:
            import hook_log
            hook_log.log('cpu-probe', type(exc).__name__)
        except Exception:
            pass


def process_role(args):
    # Retain only known role labels, never arbitrary arguments (possibly secrets).
    for role in ('renderer', 'gpu-process', 'utility', 'crashpad-handler'):
        if '--type=' + role in args:
            return role
    return 'main'


def cpu_delta(current, previous, elapsed):
    if previous is None or elapsed <= 0:
        return None
    return round(max(0, current - previous) / elapsed * 100, 2)


class Sampler:
    def __init__(self, native=True):
        import psutil
        self.ps = psutil
        self.previous = {}
        self.roles = {}
        self.native = None
        if native and os.name == 'nt':
            from cpu_probe_windows import WindowsSnapshot
            self.native = WindowsSnapshot()
        self.last = time.monotonic()
        self.ps.cpu_percent(interval=None, percpu=True)  # Discard meaningless initial reading.

    def portable_rows(self):
        rows, denied = [], 0
        for proc in self.ps.process_iter(['pid', 'name', 'create_time']):
            if (proc.info.get('name') or '').lower() not in ('code.exe', 'code', 'code-insiders.exe', 'code-insiders'):
                continue
            try:
                times = proc.cpu_times()
                rows.append(dict(pid=proc.pid, created=proc.info['create_time'],
                                 total=times.user + times.system,
                                 threads={(t.id, None):t.user_time + t.system_time for t in proc.threads()}, proc=proc))
            except self.ps.Error:
                denied += 1
        return rows, denied

    def sample(self):
        started = time.monotonic()
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        elapsed = started - self.last
        cores = self.ps.cpu_percent(interval=None, percpu=True)
        count = len(cores) or 1
        processes, threads, previous, roles = [], [], {}, {}
        rows, denied = (self.native.read(), 0) if self.native else self.portable_rows()
        for row in rows:
            identity = (row['pid'], row['created'])
            role = self.roles.get(identity)
            if role is None:
                try:
                    proc = row.get('proc') or self.ps.Process(row['pid'])
                    role = process_role(proc.cmdline())
                except self.ps.Error:
                    role = 'unknown'
            roles[identity] = role
            old = self.previous.get(identity, {})
            cpu = cpu_delta(row['total'], old.get('total'), elapsed)
            for thread_id, used in row['threads'].items():
                value = cpu_delta(used, old.get('threads', {}).get(thread_id), elapsed)
                if value is not None and value > 0:
                    threads.append(dict(pid=row['pid'], tid=thread_id[0], role=role, cpu_percent_one_core=value))
            previous[identity] = dict(total=row['total'], threads=row['threads'])
            processes.append(dict(pid=row['pid'], role=role, cpu_percent_one_core=cpu,
                                  cpu_percent_machine=round(cpu / count, 2) if cpu is not None else None))
        self.previous, self.roles, self.last = previous, roles, started
        total_cpu = sum(p['cpu_percent_one_core'] or 0 for p in processes)
        return dict(ts=time.time(), interval_sec=round(elapsed, 3), logical_cpus=count,
                    system_per_logical_cpu_percent=cores,
                    vscode_cpu_percent_machine=round(total_cpu / count, 2),
                    vscode_cpu_percent_one_core=round(total_cpu, 2),
                    processes=processes,
                    busiest_threads=sorted(threads, key=lambda t:t['cpu_percent_one_core'], reverse=True)[:8],
                    access_errors=denied, collector_cpu_ms=round((time.process_time() - cpu_started) * 1000, 2),
                    backend='windows_system_snapshot' if self.native else 'psutil',
                    collection_ms=round((time.perf_counter() - wall_started) * 1000, 2))


def active_controls(now):
    result = []
    for path in STATE.glob('*.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('expires_at', 0) > now:
                result.append(value)
        except (OSError, ValueError):
            pass
    return result


def worker():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'worker.lock').open('a+b') as lock:
        # OS releases the lock on exit/crash. Concurrent chats share one sampler.
        try:
            if os.name == 'nt':
                import msvcrt
                if lock.tell() == 0:
                    lock.write(b'0'); lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            sampler = Sampler()
            sampler.sample()  # Establish process/thread baselines too.
            idle_since = time.monotonic()
            append_record(LOG, dict(kind='cpu_probe_start', ts=time.time(), pid=os.getpid()))
            while True:
                cfg = config()
                if cfg.get('cpuProbeEnabled') is not True:
                    break
                time.sleep(number(cfg, 'cpuProbeIntervalSec', 2, 1, 60))
                active = active_controls(time.time())
                if active:
                    idle_since = time.monotonic()
                    sample = sampler.sample()
                    append_record(LOG, dict(kind='cpu_sample', active=active, **sample))
                elif time.monotonic() - idle_since > 10:
                    break
            append_record(LOG, dict(kind='cpu_probe_stop', ts=time.time()))
        except Exception as exc:
            append_record(LOG, dict(kind='cpu_probe_error', ts=time.time(), error=type(exc).__name__))


if __name__ == '__main__' and '--worker' in sys.argv:
    worker()
