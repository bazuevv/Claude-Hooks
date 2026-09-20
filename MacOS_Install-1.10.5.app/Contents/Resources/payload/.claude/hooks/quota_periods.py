"""Normalized quota history: stable period IDs and compressed usage observations."""
import json
import hashlib
import math
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def duration(window):
    value = window.get("duration_seconds")
    if numeric(value) and value > 0:
        return value
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ч|дн)", window.get("label") or "")
    return float(match[1]) * (3600 if match[2] == "ч" else 86400) if match else None


def connect(root):
    db = sqlite3.connect(root / "quota-periods.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript('''
        CREATE TABLE IF NOT EXISTS aliases (legacy TEXT PRIMARY KEY, account TEXT);
        CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER);
        CREATE TABLE IF NOT EXISTS quota_cycles (
          id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT, window TEXT NOT NULL,
          label TEXT, duration REAL, start REAL, end REAL, first_seen REAL, last_seen REAL);
        CREATE INDEX IF NOT EXISTS cycle_account ON quota_cycles(account, window, first_seen);
        CREATE TABLE IF NOT EXISTS quota_samples (
          period_id TEXT NOT NULL REFERENCES quota_cycles(id) ON DELETE CASCADE,
          first_seen REAL, last_seen REAL, used REAL, expired INTEGER,
          PRIMARY KEY(period_id, first_seen));
        CREATE TABLE IF NOT EXISTS quota_revisions (
          period_id TEXT NOT NULL REFERENCES quota_cycles(id) ON DELETE CASCADE,
          observed_at REAL, previous_end REAL, end REAL,
          UNIQUE(period_id, observed_at, previous_end, end));
        CREATE TABLE IF NOT EXISTS subscriptions (
          account TEXT, start TEXT, end TEXT, PRIMARY KEY(account, start, end));
    ''')
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute('PRAGMA table_info(quota_samples)')}
            for name, spec in (("source", "TEXT NOT NULL DEFAULT 'external'"), ("task_id", "TEXT"),
                               ("phase", "TEXT"), ("task_started_at", "REAL")):
                if name not in columns:
                    db.execute(f'ALTER TABLE quota_samples ADD COLUMN {name} {spec}')
            if db.execute('PRAGMA user_version').fetchone()[0] < 4:
                db.execute('DELETE FROM imports')  # Re-import genuine hook provenance.
            db.execute('DROP TABLE IF EXISTS quota_attributions')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='observations'").fetchone():
                rows = db.execute("SELECT * FROM observations ORDER BY observed, id").fetchall()
                for row in rows:
                    _record_window(db, _canonical(db, row['account']), row['provider'], {
                        'key': row['window'], 'label': row['label'], 'duration_seconds': row['duration'],
                        'observed_at': row['observed'], 'reset_at': row['reset'],
                        'used': row['used'], 'expired': row['expired']})
                db.execute("DROP TABLE observations")
                db.execute("DROP TABLE IF EXISTS periods")
                db.execute("DELETE FROM imports")
            db.execute("PRAGMA user_version=4")
    except Exception:
        db.close()
        raise
    return db


def _canonical(db, account):
    seen = set()
    while account and account not in seen:
        seen.add(account)
        row = db.execute("SELECT account FROM aliases WHERE legacy=?", (account,)).fetchone()
        if not row:
            break
        account = row[0]
    return account


def _points(db, period_id):
    points = {}
    for row in db.execute("SELECT * FROM quota_samples WHERE period_id=? ORDER BY first_seen", (period_id,)):
        for stamp in (row['first_seen'], row['last_seen']):
            points[stamp] = tuple(row[k] for k in ('used', 'expired', 'source', 'task_id', 'phase', 'task_started_at'))
    return points


def _compact(db, period_id, points):
    runs = []
    for stamp, state in sorted(points.items()):
        if runs and runs[-1][3:] == list(state):
            runs[-1][2] = stamp
        else:
            runs.append([period_id, stamp, stamp, *state])
    db.execute("DELETE FROM quota_samples WHERE period_id=?", (period_id,))
    db.executemany("INSERT INTO quota_samples VALUES (?,?,?,?,?,?,?,?,?)", runs)


def task_source(event, start=None):
    """Called only for actual Claude Code message journals, never quota polling."""
    if (event.get('billing') != 'subscription' or not event.get('account')
            or not event.get('transcript') or not event.get('id')
            or event.get('phase') not in ('start', 'progress', 'end')):
        return event
    start = event if event['phase'] == 'start' else start or {}
    task = str(start.get('submission_id') or start.get('id') or event['id'])
    task_id = hashlib.sha256((str(event['transcript']) + '|' + task).encode()).hexdigest()
    baseline = start.get('captured_at') if start.get('account') == event['account'] else None
    return {**event, 'source': 'claude-code', 'task_id': task_id, 'task_started_at': baseline}


def _prefer_source(existing, incoming):
    # Re-importing a plain snapshot must not erase the same hook observation.
    if existing and existing[:2] == incoming[:2]:
        def rank(state):
            if state[2] != 'claude-code':
                return 0
            return 2 if state[4] in ('progress', 'end') and numeric(state[5]) else 1
        if rank(existing) > rank(incoming):
            return existing
    return incoming


def _record_window(db, account, provider, window):
    observed, used = window.get('observed_at'), window.get('used')
    key = window.get('key') or window.get('label')
    if not account or not isinstance(key, str) or not numeric(observed) or not numeric(used) or not 0 <= used <= 100:
        return
    seconds, reset = duration(window), window.get('reset_at')
    reset = reset if numeric(reset) else None
    expired = int(bool(window.get('expired')))
    source = window.get('source') if window.get('source') in ('claude-code', 'accs', 'background') else 'external'
    state = (used, expired, source, window.get('task_id'), window.get('phase'), window.get('task_started_at'))
    rows = db.execute("SELECT * FROM quota_cycles WHERE account=? AND window=? ORDER BY first_seen", (account, key)).fetchall()
    period = next((p for p in rows if p['duration'] == seconds and
                   ((p['end'] is None and reset is None) or
                    (p['end'] is not None and reset is not None and abs(p['end'] - reset) <= 2))), None)
    if period is None and reset is not None:
        period = next((p for p in rows if p['duration'] == seconds and db.execute(
            "SELECT 1 FROM quota_revisions WHERE period_id=? AND ABS(previous_end-?)<=2",
            (p['id'], reset)).fetchone()), None)
    if period is None and reset is not None and reset > observed and not expired:
        period = next((p for p in reversed(rows) if p['duration'] == seconds and p['end'] is not None
                       and p['first_seen'] <= observed < p['end'] - 2), None)
        if period is not None:
            db.execute("INSERT OR IGNORE INTO quota_revisions VALUES (?,?,?,?)", (period['id'], observed, period['end'], reset))
            db.execute("UPDATE quota_cycles SET start=?, end=? WHERE id=?", (reset - seconds if seconds else None, reset, period['id']))
    if period is None:
        period_id = uuid.uuid4().hex
        db.execute("INSERT INTO quota_cycles VALUES (?,?,?,?,?,?,?,?,?,?)", (
            period_id, account, provider, key, window.get('label', key), seconds,
            reset - seconds if reset is not None and seconds else None, reset, observed, observed))
    else:
        period_id = period['id']
        db.execute("UPDATE quota_cycles SET first_seen=MIN(first_seen,?), last_seen=MAX(last_seen,?) WHERE id=?",
                   (observed, observed, period_id))
    latest = db.execute("SELECT * FROM quota_samples WHERE period_id=? ORDER BY first_seen DESC LIMIT 1", (period_id,)).fetchone()
    if latest and observed >= latest['last_seen'] and state == tuple(latest[k] for k in ('used', 'expired', 'source', 'task_id', 'phase', 'task_started_at')):
        if observed > latest['last_seen']:
            db.execute("UPDATE quota_samples SET last_seen=? WHERE period_id=? AND first_seen=?", (observed, period_id, latest['first_seen']))
        return
    points = _points(db, period_id)
    points[observed] = _prefer_source(points.get(observed), state)
    _compact(db, period_id, points)


def _insert(db, payload, *, task_journal=False):
    events = []
    for line in payload.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get('account'):
            events.append(event)
    if task_journal:
        starts = {(e.get('transcript'), e.get('id')): e for e in events if e.get('phase') == 'start'}
        events = [task_source(e, starts.get((e.get('transcript'), e.get('id')))) for e in events]
    for event in events:
        account = _canonical(db, event['account'])
        for alias in event.get('legacy_accounts', []):
            if isinstance(alias, str) and alias != account:
                db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (alias, account))
                for old in db.execute('SELECT * FROM quota_cycles WHERE account=?', (alias,)).fetchall():
                    target = db.execute('''SELECT * FROM quota_cycles WHERE account=? AND window=?
                        AND duration IS ? AND (end IS ? OR ABS(end-?)<=2) ORDER BY first_seen LIMIT 1''',
                        (account, old['window'], old['duration'], old['end'], old['end'])).fetchone()
                    if target:
                        points = _points(db, old['id'])
                        for stamp, state in _points(db, target['id']).items():
                            points[stamp] = _prefer_source(points.get(stamp), state)
                        _compact(db, target['id'], points)
                        db.execute('UPDATE quota_cycles SET first_seen=MIN(first_seen,?),last_seen=MAX(last_seen,?) WHERE id=?',
                                   (old['first_seen'], old['last_seen'], target['id']))
                        db.execute('INSERT OR IGNORE INTO quota_revisions SELECT ?,observed_at,previous_end,end FROM quota_revisions WHERE period_id=?', (target['id'], old['id']))
                        db.execute('DELETE FROM quota_cycles WHERE id=?', (old['id'],))
                db.execute("UPDATE quota_cycles SET account=? WHERE account=?", (account, alias))
                db.execute("INSERT OR IGNORE INTO subscriptions SELECT ?,start,end FROM subscriptions WHERE account=?", (account, alias))
                db.execute("DELETE FROM subscriptions WHERE account=?", (alias,))
        paid = event.get('subscriptionPeriod')
        if isinstance(paid, dict):
            try:
                if datetime.fromisoformat(paid['start']) < datetime.fromisoformat(paid['end']):
                    db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)", (account, paid['start'], paid['end']))
            except (ValueError, TypeError, KeyError):
                pass
    windows = [(w, e) for e in events if e.get('billing') == 'subscription'
               for w in e.get('windows', []) if isinstance(w, dict) and numeric(w.get('observed_at'))]
    for window, event in sorted(windows, key=lambda pair: pair[0]['observed_at']):
        metadata = {key: event.get(key) for key in ('source', 'task_id', 'phase', 'task_started_at')}
        _record_window(db, _canonical(db, event['account']), event.get('provider', ''), {**window, **metadata})


def ingest(root, payload):
    with closing(connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _insert(db, payload)


def sync(root, sources=()):
    """Import legacy journals once per change; new account samples use SQLite."""
    with closing(connect(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for path in [root / 'account-usage-samples.jsonl', *root.glob('message-costs-*.jsonl'), *sources]:
            if not path.is_file():
                continue
            stat = path.stat()
            key = str(path.resolve())
            old = db.execute("SELECT size,mtime FROM imports WHERE path=?", (key,)).fetchone()
            if old and (old['size'], old['mtime']) == (stat.st_size, stat.st_mtime_ns):
                continue
            payload = path.read_bytes()
            _insert(db, payload[:payload.rfind(b'\n') + 1], task_journal=path.name.startswith('message-costs-'))
            db.execute("INSERT OR REPLACE INTO imports VALUES (?,?,?)", (key, stat.st_size, stat.st_mtime_ns))


def account_ids(root, account, aliases=()):
    with closing(connect(root)) as db:
        ids = {v for v in (account, *aliases) if v}
        ids.update(row[0] for row in db.execute("SELECT legacy FROM aliases WHERE account=?", (account,)))
        return ids


def subscription_periods(root, account):
    with closing(connect(root)) as db:
        return [dict(r) for r in db.execute("SELECT start,end FROM subscriptions WHERE account=? ORDER BY start", (_canonical(db, account),))]


def cycles(root, account, aliases=()):
    if not account:
        return []
    result = []
    with closing(connect(root)) as db:
        for row in db.execute("SELECT * FROM quota_cycles WHERE account=? ORDER BY first_seen", (_canonical(db, account),)):
            if row['end'] is None or row['duration'] is None:
                continue
            points = _points(db, row['id'])
            samples = [{'observed_at': stamp, 'used': state[0], 'source': state[2],
                        'task_id': state[3], 'phase': state[4], 'task_started_at': state[5]}
                       for stamp, state in sorted(points.items()) if not state[1] and stamp < row['end']]
            if not samples:
                continue
            result.append({'id': row['id'], 'account': account, 'provider': row['provider'], 'window': row['window'],
                           'label': row['label'], 'duration': row['duration'], 'start': row['start'], 'end': row['end'],
                           'samples': samples, 'used': max(s['used'] for s in samples), 'status': 'observed',
                           'revisions': [dict(r) for r in db.execute('SELECT observed_at,previous_end,end FROM quota_revisions WHERE period_id=? ORDER BY observed_at', (row['id'],))]})
    previous = {}
    for period in result:
        before = previous.get(period['window'])
        if before and period['samples'][0]['observed_at'] >= before['end'] - 2 and period['end'] > before['end']:
            before['status'] = 'reset_confirmed'
        previous[period['window']] = period
    return result
