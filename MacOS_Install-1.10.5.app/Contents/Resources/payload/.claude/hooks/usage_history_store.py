"""Persistent usage journals, independent of disposable hook runtime files."""
import os
import threading
from contextlib import contextmanager
from pathlib import Path
import quota_periods

_LOCK = threading.RLock()
_MIGRATED = {}
ROOT = Path.home() / ".claude" / "usage-history"


def directory(state_dir):
    return ROOT


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with _LOCK, (root / ".write.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def prepare(state_dir):
    """Copy complete legacy records without deleting originals or replacing newer data.

    Migration is repeatable, including after a restart or late writes by an old
    process. New records and migration share a cross-process lock. The legacy
    files remain as a backup until the runtime directory is cleaned normally.
    """
    root = directory(state_dir)
    legacy = Path(state_dir)
    with locked(root):
        former = legacy.with_name("usage-history") if legacy.name == "hooks-runtime" else legacy / "usage-history"
        locations = {legacy.absolute(), former.absolute()}
        sources = [p for folder in locations if folder != root.absolute()
                   for p in [folder / "account-usage-samples.jsonl", *folder.glob("message-costs-*.jsonl")]]
        for source in sources:
            if source.name == "account-usage-samples.jsonl":
                continue  # Import directly into SQLite; do not recreate the old journal.
            if not source.is_file():
                continue
            stat = source.stat()
            key = str(source.absolute())
            signature = (stat.st_mtime_ns, stat.st_size)
            target = root / source.name
            if _MIGRATED.get(key) == signature and target.exists():
                continue
            old = source.read_bytes().splitlines(keepends=True)
            existing = target.read_bytes() if target.exists() else b""
            seen = set()
            prefix = []
            for line in old:
                if not line.endswith(b"\n") or line.rstrip(b"\r\n") in seen:
                    continue
                seen.add(line.rstrip(b"\r\n"))
                prefix.append(line)
            if seen - set(existing.splitlines()):
                # Keep the original legacy order when its last partial record
                # becomes complete later. New destination-only records remain last.
                tail = b"".join(line for line in existing.splitlines(keepends=True)
                                if line.rstrip(b"\r\n") not in seen)
                temporary = target.with_suffix(".migrating")
                with temporary.open("wb") as output:
                    output.write(b"".join(prefix) + tail)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            _MIGRATED[key] = signature
        backups = root / "legacy"
        quota_periods.sync(root, [p for p in sources if p.name == "account-usage-samples.jsonl"]
                           + list(backups.glob("account-usage-samples-*.jsonl")))
        old_samples = root / "account-usage-samples.jsonl"
        if old_samples.is_file():
            backups.mkdir(exist_ok=True)
            backup = backups / ("account-usage-samples-" + str(old_samples.stat().st_mtime_ns) + ".jsonl")
            # The successful transaction above precedes retiring the old format.
            os.replace(old_samples, backup)
    return root


def append(state_dir, filename, payload):
    root = prepare(state_dir)
    with locked(root):
        path = root / filename
        with path.open("ab") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        quota_periods.ingest(root, payload)
    return path


def record_account(state_dir, sample):
    import json
    root = prepare(state_dir)
    with locked(root):
        quota_periods.ingest(root, (json.dumps(sample) + "\n").encode("utf-8"))
    return root / "quota-periods.sqlite3"
