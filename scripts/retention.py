"""Data retention and deletion (plan section 18).

Applies per-company retention windows to task, audit, archive, worker, history-access and
backup artifacts. Terminal task rows and their audit/archive entries may be deleted; tasks
that are still in flight are never touched. Dry-run by default.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULTS = {'tasks_days': 30, 'audit_days': 180, 'archive_days': 365,
            'history_access_days': 180, 'worker_days': 30, 'backups_days': 30, 'budget_days': 180}
TERMINAL = ('succeeded', 'failed', 'expired', 'cancelled')
DAY = 86400.0


def policy(runtime=None):
    merged = dict(DEFAULTS)
    try:
        from app import site
        merged.update(site.load(runtime).get('retention', {}))
    except Exception:
        pass
    return merged


def _tables(db, names):
    found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return found & set(names)


def _plan_local(runtime, windows=None, backups_dir=None, now=None):
    runtime = Path(runtime); windows = windows or policy(runtime); now = now or time.time()
    cut = {key: now - days * DAY for key, days in windows.items()}
    report = {'cutoffs_days': windows, 'tasks': 0, 'task_audit': 0, 'task_archive': 0,
              'history_events': 0, 'history_requests': 0, 'worker_done': 0, 'budget_usage': 0, 'budget_denials': 0, 'backups': []}
    db_path = runtime / 'tasks.sqlite3'
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as db:
            if 'tasks' in _tables(db, ['tasks']):
                report['tasks'] = db.execute(
                    f"SELECT count(*) FROM tasks WHERE state IN ({','.join('?' * len(TERMINAL))}) AND created_at < ?",
                    (*TERMINAL, cut['tasks_days'])).fetchone()[0]
            if 'task_audit' in _tables(db, ['task_audit']):
                report['task_audit'] = db.execute('SELECT count(*) FROM task_audit WHERE timestamp < ?',
                                                  (cut['audit_days'],)).fetchone()[0]
            if 'task_archive' in _tables(db, ['task_archive']):
                report['task_archive'] = db.execute('SELECT count(*) FROM task_archive WHERE archived_at < ?',
                                                    (cut['archive_days'],)).fetchone()[0]
    history = runtime / 'history-access.sqlite3'
    if history.exists():
        with closing(sqlite3.connect(history)) as db:
            tables = _tables(db, ['requests', 'events'])
            if 'events' in tables:
                report['history_events'] = db.execute('SELECT count(*) FROM events WHERE time < ?',
                                                      (cut['audit_days'],)).fetchone()[0]
            if 'requests' in tables:
                report['history_requests'] = db.execute("SELECT count(*) FROM requests WHERE state != 'denied' AND expires > 0 AND expires < ?",
                                                        (cut['history_access_days'],)).fetchone()[0]
    budget = runtime / 'model-budget.sqlite3'
    if budget.exists():
        cutoff_day = time.strftime('%Y-%m-%d', time.gmtime(min(cut['budget_days'], now - DAY)))
        with closing(sqlite3.connect(budget)) as db:
            report['budget_usage'] = db.execute('SELECT count(*) FROM usage WHERE day < ?', (cutoff_day,)).fetchone()[0]
            report['budget_denials'] = db.execute('SELECT count(*) FROM denials WHERE time < ?', (cut['audit_days'],)).fetchone()[0]
    for worker in runtime.glob('worker-*.sqlite3'):
        with closing(sqlite3.connect(worker)) as db:
            if 'work' in _tables(db, ['work']):
                report['worker_done'] += db.execute(
                    "SELECT count(*) FROM work WHERE state='done' AND updated_at < ?", (cut['worker_days'],)).fetchone()[0]
    if backups_dir:
        backups_dir = Path(backups_dir)
        if backups_dir.is_dir():
            report['backups'] = sorted(str(p) for p in backups_dir.iterdir()
                                       if p.stat().st_mtime < cut['backups_days'])
    return report


def _apply_local(runtime, windows=None, confirm=False, backups_dir=None, now=None):
    if not confirm:
        raise ValueError('Refusing to delete without confirm=True')
    runtime = Path(runtime); windows = windows or policy(runtime); now = now or time.time()
    cut = {key: now - days * DAY for key, days in windows.items()}
    report = {'deleted': {'tasks': 0, 'task_audit': 0, 'task_archive': 0,
                          'history_events': 0, 'history_requests': 0, 'worker_done': 0, 'budget_usage': 0, 'budget_denials': 0, 'backups': 0}}
    db_path = runtime / 'tasks.sqlite3'
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as db, db:
            if 'tasks' in _tables(db, ['tasks']):
                cur = db.execute(f"DELETE FROM tasks WHERE state IN ({','.join('?' * len(TERMINAL))}) AND created_at < ?",
                                 (*TERMINAL, cut['tasks_days']))
                report['deleted']['tasks'] = cur.rowcount
            if 'task_audit' in _tables(db, ['task_audit']):
                report['deleted']['task_audit'] = db.execute('DELETE FROM task_audit WHERE timestamp < ?',
                                                             (cut['audit_days'],)).rowcount
            if 'task_archive' in _tables(db, ['task_archive']):
                report['deleted']['task_archive'] = db.execute('DELETE FROM task_archive WHERE archived_at < ?',
                                                               (cut['archive_days'],)).rowcount
    history = runtime / 'history-access.sqlite3'
    if history.exists():
        with closing(sqlite3.connect(history)) as db, db:
            tables = _tables(db, ['requests', 'events'])
            if 'events' in tables:
                report['deleted']['history_events'] = db.execute('DELETE FROM events WHERE time < ?',
                                                                 (cut['audit_days'],)).rowcount
            if 'requests' in tables:
                # A denied row is a durable administrative veto, not an expired grant.
                # Removing it changes DENIED to UNMANAGED and can re-enable device consent.
                report['deleted']['history_requests'] = db.execute("DELETE FROM requests WHERE state != 'denied' AND expires > 0 AND expires < ?",
                                                                   (cut['history_access_days'],)).rowcount
    budget = runtime / 'model-budget.sqlite3'
    if budget.exists():
        cutoff_day = time.strftime('%Y-%m-%d', time.gmtime(min(cut['budget_days'], now - DAY)))
        with closing(sqlite3.connect(budget)) as db, db:
            report['deleted']['budget_usage'] = db.execute('DELETE FROM usage WHERE day < ?', (cutoff_day,)).rowcount
            report['deleted']['budget_denials'] = db.execute('DELETE FROM denials WHERE time < ?', (cut['audit_days'],)).rowcount
    for worker in runtime.glob('worker-*.sqlite3'):
        with closing(sqlite3.connect(worker)) as db, db:
            if 'work' in _tables(db, ['work']):
                report['deleted']['worker_done'] += db.execute(
                    "DELETE FROM work WHERE state='done' AND updated_at < ?", (cut['worker_days'],)).rowcount
    if backups_dir:
        backups_dir = Path(backups_dir)
        if backups_dir.is_dir():
            for path in list(backups_dir.iterdir()):
                if path.is_dir() and path.stat().st_mtime < cut['backups_days']:
                    import shutil
                    shutil.rmtree(path); report['deleted']['backups'] += 1
    report['applied_at'] = datetime.now(timezone.utc).isoformat()
    return report


def _context(runtime, windows):
    from app.common import authority_base
    runtime = Path(runtime).resolve()
    authority = Path(authority_base(runtime)).resolve()
    if not runtime.is_dir() or not authority.is_dir():
        raise ValueError('Runtime and authority directories must exist')
    resolved = {**DEFAULTS, **(windows or policy(runtime))}
    if any(not isinstance(v, (int, float)) or not 0 < v < float('inf') for v in resolved.values()):
        raise ValueError('Retention windows must be finite and positive')
    return list(dict.fromkeys([runtime, authority])), resolved


def inventory(runtime):
    """Every discovered data class has a lifecycle, or an explicit coverage blocker.

    Active authority/records, idempotency tombstones and audit evidence are retained;
    their removal needs a data-owner policy and must never silently change rights.
    Retired artifacts are deleted only from explicitly configured, held-aware roots.
    """
    from scripts.runtime_backup import NAME
    bases, _ = _context(runtime, None)
    domains = []
    blockers = []
    for base in bases:
        for path in sorted(base.glob('*.sqlite3')):
            if not NAME.fullmatch(path.name):
                blockers.append('unclassified database: ' + str(path))
                continue
            mode = 'active-control-retained'
            if path.name == 'tasks.sqlite3':mode = 'terminal-task-and-audit-windows'
            elif path.name.startswith('worker-'):mode = 'completed-work-window'
            elif path.name == 'history-access.sqlite3':mode = 'expired-grants-and-events; explicit-denials-retained'
            elif path.name == 'model-budget.sqlite3':mode = 'past-day-usage-and-denial-audit-window'
            domains.append({'path': str(path), 'policy': mode})
        for directory in ('project-records', 'jetstream', 'artifacts', 'objects', 'audit', 'health'):
            if (base / directory).exists():
                domains.append({'path':str(base/directory), 'policy':'active-evidence-retained; retired-roots-require-explicit-policy'})
        for path in sorted(base.iterdir()):
            if path.is_file() and path.suffix != '.sqlite3':
                mode = 'active-operational-file-retained'
                if path.suffix == '.jsonl' or '.chain.' in path.name:
                    mode = 'active-audit-evidence-retained; verified-retirement-required'
                elif '.log' in path.name:
                    mode = 'size-rotation; retired-roots-require-explicit-policy'
                domains.append({'path':str(path), 'policy':mode})
    path = Path(runtime) / 'lifecycle.json'
    config = json.loads(path.read_text()) if path.exists() else {'retired_roots': []}
    roots = config.get('retired_roots', [])
    if not isinstance(roots, list):raise ValueError('retired_roots must be a list')
    for root in roots:
        relative = Path(root['path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0] not in ('retired-data','audit-archive','project-archive','artifact-archive','object-archive'):
            raise ValueError('Retired root must be an approved archive inside the local runtime')
        if not isinstance(root.get('days'), (int,float)) or not 0 < root['days'] < float('inf') or not isinstance(root.get('legal_hold'), bool):
            raise ValueError('Retired roots require positive days and explicit legal_hold')
        domains.append({'path':str(Path(runtime)/relative), 'policy':'legal-hold' if root['legal_hold'] else 'retired-file-window'})
    return {'domains':domains, 'retired_roots':roots, 'blockers':blockers, 'coverage_complete':not blockers,
            'note':'Active rights, current project records and audit evidence are preserved. Company retention and legal-hold approval is a separate production requirement.'}


def _retired(runtime, roots, now, delete=False):
    base = Path(runtime).resolve()
    selected = []
    for row in roots:
        if row['legal_hold']:continue
        directory = base / row['path']
        if directory.is_symlink() or not directory.resolve().is_relative_to(base):raise ValueError('Archive root escapes runtime')
        for path in sorted(directory.rglob('*')):
            if path.is_symlink() or not path.is_file():continue
            if not path.resolve().is_relative_to(directory.resolve()):raise ValueError('Archive file escapes root')
            if path.stat().st_mtime < now - row['days'] * DAY:
                selected.append(str(path))
    if delete:
        for path in dict.fromkeys(selected):Path(path).unlink()
    return sorted(set(selected))


def plan(runtime, windows=None, backups_dir=None, now=None):
    bases, windows = _context(runtime, windows)
    now = time.time() if now is None else now
    result = _plan_local(bases[0], windows, backups_dir, now)
    for base in bases[1:]:
        extra = _plan_local(base, windows, None, now)
        for key, value in extra.items():
            if isinstance(value, int):result[key] += value
    result['coverage'] = inventory(runtime)
    result['retired_files'] = _retired(runtime, result['coverage']['retired_roots'], now)
    result['authority_directories'] = [str(base) for base in bases]
    result['kv_required'] = os.environ.get('POC_HA') == '1'
    return result


def apply(runtime, windows=None, confirm=False, backups_dir=None, now=None, kv_applied=False):
    if not confirm:raise ValueError('Refusing to delete without confirm=True')
    if os.environ.get('POC_HA') == '1' and not kv_applied:
        raise ValueError('HA retention requires --with-kv; local SQLite cleanup is not sufficient')
    bases, windows = _context(runtime, windows)
    now = time.time() if now is None else now
    coverage = inventory(runtime)  # validate configuration before deleting anything
    _retired(runtime, coverage['retired_roots'], now)  # validate exact archive roots first
    result = _apply_local(bases[0], windows, True, backups_dir, now)
    for base in bases[1:]:
        extra = _apply_local(base, windows, True, None, now)
        for key,value in extra['deleted'].items():result['deleted'][key] += value
    result['coverage'] = coverage
    result['deleted']['retired_files'] = len(_retired(runtime, coverage['retired_roots'], now, True))
    return result


async def kv_retention(identity, windows, apply=False):
    from app.common import connect
    from app.kv_task_store import KvTaskStore
    from app import site
    from nats.js.api import StreamConfig
    nc = await connect(identity, timeout=10)
    try:
        js = nc.jetstream()
        store = KvTaskStore(js)
        result = await store.enforce_quota(before=time.time()-windows['tasks_days']*DAY) if apply else await store.quota_status()
        ttl_days = site.load().get('retention', {}).get('jetstream_days', 1)
        for name in ('TASKS','TASKSDLQ'):
            info = await js.stream_info(name)
            if apply and info.config.max_age != ttl_days * DAY:
                # Update only the existing stream's TTL, preserving replicas/subjects/config.
                config = info.config; config.max_age = ttl_days * DAY
                await js.update_stream(config=config)
            result[name] = {'current_max_age_seconds':info.config.max_age, 'requested_max_age_seconds':ttl_days*DAY}
        return result
    finally:
        await nc.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--backups-dir', type=Path)
    parser.add_argument('--days', type=int, help='override every window with this value')
    parser.add_argument('--with-kv', action='store_true', help='include authoritative KV and stream TTL policies')
    parser.add_argument('--identity', default='router')
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--apply', action='store_true', help='actually delete (default is a dry run)')
    args = parser.parse_args()
    os.environ['POC_RUNTIME'] = str(args.runtime.resolve())
    if not os.environ.get('NATS_URL') and (args.runtime/'endpoint.txt').exists():
        os.environ['NATS_URL'] = (args.runtime/'endpoint.txt').read_text().strip()
    windows = policy(args.runtime)
    if args.days is not None:
        windows = {key: args.days for key in windows}
    preview = plan(args.runtime, windows, backups_dir=args.backups_dir)
    if args.require_complete and preview['coverage']['blockers']:
        print(json.dumps(preview, ensure_ascii=False, indent=2)); return 1
    kv = None
    if args.with_kv:
        import asyncio
        kv = asyncio.run(kv_retention(args.identity, {**DEFAULTS, **windows}, args.apply))
    result = apply(args.runtime, windows, confirm=True, backups_dir=args.backups_dir, kv_applied=kv is not None) if args.apply else preview
    if kv is not None:result['kv'] = kv
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
