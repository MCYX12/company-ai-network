"""Local disaster-recovery drill: backup -> verify -> restore -> verify, with RTO/RPO.

    python -m scripts.dr_drill --runtime <runtime> --workdir <dir> [--include-credentials]

RTO here measures local SQLite restore and integrity checks. Recovery point age describes the
newly captured snapshot; newest_data_age is separate. Neither measurement proves production
business RTO/RPO. Native JetStream archives need isolated restore/reconciliation separately.
"""
import argparse
from datetime import datetime, timezone
import json
from contextlib import closing
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import runtime_backup


def newest_task_stamp(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT max(created_at) FROM tasks').fetchone()
    return row[0] if row and row[0] is not None else None


def run(runtime, workdir, include_credentials=False, authority_runtime=None, jetstream_snapshot=None):
    runtime = Path(runtime).resolve()
    workdir = Path(workdir).absolute()
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / 'snapshot'
    backup_started = time.time()
    backup = runtime_backup.backup(runtime, snapshot, include_credentials=include_credentials,
                                   authority_runtime=authority_runtime, jetstream_snapshot=jetstream_snapshot)
    backup_seconds = time.time() - backup_started
    verify = runtime_backup.verify(snapshot)
    restore_started = time.time()
    destination = workdir / 'restored'
    report = runtime_backup.restore(snapshot, destination)
    checked = {}
    for path in sorted(destination.rglob('*.sqlite3')):
        try:
            checked[path.relative_to(destination).as_posix()] = runtime_backup.check_db(path, path.name)
        except ValueError:
            checked[path.relative_to(destination).as_posix()] = 'invalid'
    rto_seconds = time.time() - restore_started
    authoritative = destination / backup.get('authority_layout', '.')
    tasks = authoritative / 'tasks.sqlite3'
    stamp = newest_task_stamp(tasks if tasks.exists() else destination / 'tasks.sqlite3')
    invalid = sorted(name for name, value in checked.items() if value == 'invalid')
    created = backup.get('created_at')
    try:
        snapshot_age = (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds()
    except (TypeError, ValueError):
        snapshot_age = None
    expected = ('accounts.sqlite3', 'roster.sqlite3', 'history-access.sqlite3', 'replay.sqlite3')
    return {'runtime': str(runtime),
            'snapshot_created_at': created,
            'backup_seconds': round(backup_seconds, 3),
            'snapshot_files': len(verify.get('files') or []),
            'restore_report': report,
            'rto_seconds': round(rto_seconds, 3),
            # RPO is the age of the last recoverable snapshot (data since then would be lost);
            # newest_data_age is a separate observation, not the RPO.
            'recovery_point_seconds': round(snapshot_age, 3) if snapshot_age is not None else None,
            'newest_data_age_seconds': round(time.time() - stamp, 3) if stamp is not None else None,
            'restored_databases': checked,
            'invalid_databases': invalid,
            'expected_authority_databases': list(expected),
            'missing_authority_databases': [name for name in expected if not (authoritative / name).exists()],
            'backup_coverage_complete': backup.get('coverage_complete', False),
            'excluded_databases': backup.get('excluded_databases', []),
            'jetstream_included': backup.get('jetstream_included', False),
            'measurement_scope': 'local SQLite restore and integrity checks; snapshot age is not production RPO',
            'authority_restored': {key: report.get('ledgers', {}).get(key)
                                   for key in ('employment', 'history_requests', 'replay_seen')}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--workdir', type=Path, required=True)
    parser.add_argument('--include-credentials', action='store_true')
    parser.add_argument('--authority-runtime', type=Path)
    parser.add_argument('--jetstream-snapshot', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--allow-missing', action='store_true',
                        help='do not fail when an expected authority database is absent')
    args = parser.parse_args()
    result = run(args.runtime, args.workdir, args.include_credentials, args.authority_runtime, args.jetstream_snapshot)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + '\n')
    print(text)
    # A drill that restored an unreadable database, or could not restore an expected authority
    # database, is a failure unless the caller explicitly allows a missing store.
    failed = bool(result['invalid_databases']) or not result['backup_coverage_complete']
    if not args.allow_missing and result['missing_authority_databases']:
        failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
