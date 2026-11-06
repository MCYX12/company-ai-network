"""Local operations drill: alert delivery, off-site anchor, retention, RTO/RPO targets.

    python -m scripts.ops_drill --runtime <runtime> --workdir <dir>
        [--rto-target 60] [--rpo-target 86400] [--retention-days 30]

These are local, reproducible checks of the operations contract. On-call delivery to a real
pager/chat, a genuine WORM target and off-site DR still need the field.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import audit_chain, dr_drill


def alert_delivery(spool_dir, alert):
    """Transactional local receiver simulation, deduplicated by stable alert ID.

    This is not a real pager receipt. SQLite owns the delivery/receipt pair; JSONL
    projections are regenerated, so a crash between writes can be repaired on retry.
    """
    import fcntl
    import sqlite3
    import tempfile
    ident = alert.get('id')
    if not isinstance(ident, str) or not ident.strip():
        raise ValueError('A stable nonempty alert id is required')
    spool_dir = Path(spool_dir)
    spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log = spool_dir / 'alerts.jsonl'
    receipts = spool_dir / 'receipts.jsonl'
    def rows(path):
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
    def project(path, values):
        fd, temporary = tempfile.mkstemp(prefix='.alert-', dir=spool_dir)
        try:
            with os.fdopen(fd, 'w') as handle:
                for value in values:
                    handle.write(json.dumps(value, ensure_ascii=False) + '\n')
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
    lock_path = spool_dir / 'delivery.lock'
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db_path = spool_dir / 'delivery.sqlite3'
        db_fd = os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600); os.close(db_fd)
        from contextlib import closing
        with closing(sqlite3.connect(db_path)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, payload TEXT, receipt TEXT)')
            with db:
                # Import legacy spool files without treating a missing receipt as success.
                legacy_receipts = {row.get('alert'): row for row in rows(receipts)}
                for row in rows(log):
                    if row.get('id'):
                        receipt = legacy_receipts.get(row['id'])
                        db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?)',
                                   (row['id'], json.dumps(row), json.dumps(receipt) if receipt else None))
                old = db.execute('SELECT payload,receipt FROM deliveries WHERE id=?', (ident,)).fetchone()
                deduplicated = old is not None
                receipt = json.dumps({'alert': ident, 'at': time.time(), 'receiver': 'local-simulation'})
                if old is None:
                    db.execute('INSERT INTO deliveries VALUES(?,?,?)', (ident, json.dumps(alert), receipt))
                elif not old[1]:
                    db.execute('UPDATE deliveries SET receipt=? WHERE id=?', (receipt, ident))
            saved = db.execute('SELECT payload,receipt FROM deliveries ORDER BY rowid').fetchall()
            project(log, [json.loads(row[0]) for row in saved])
            project(receipts, [json.loads(row[1]) for row in saved if row[1]])
        return {'delivered': True, 'copies': 1, 'deduplicated': deduplicated,
                'receipts': sum(bool(row[1]) for row in saved), 'receiver': 'local-simulation'}


def offsite_anchor(audit, primary_worm, offsite_dir):
    """Verify an anchor from a second (off-site) copy after the primary is lost."""
    audit = Path(audit)
    primary_worm = Path(primary_worm)
    offsite_dir = Path(offsite_dir)
    offsite_dir.mkdir(parents=True, exist_ok=True)
    audit_chain.export_worm(audit, primary_worm)
    copy = offsite_dir / 'anchors.jsonl'
    shutil.copyfile(audit_chain.worm_log(primary_worm), copy)
    before = audit_chain.check_worm(audit, offsite_dir)['ok']
    shutil.rmtree(primary_worm, ignore_errors=True)
    after = audit_chain.check_worm(audit, offsite_dir)['ok']
    return {'offsite_ok_before_loss': before, 'offsite_ok_after_loss': after}


def retention(directory, days, now=None):
    """Delete files older than the retention window; keep newer ones."""
    directory = Path(directory)
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    deleted, kept = [], []
    victims, survivors = [], []
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or path.is_symlink():
            continue
        (victims if path.stat().st_mtime < cutoff else survivors).append(path)
    deleted = [path.name for path in victims]
    kept = [path.name for path in survivors]
    for path in victims:
        # Delete the exact selected path: never re-search by basename, which deleted a fresh
        # same-named file in a sibling directory.
        path.unlink()
    return {'deleted': sorted(deleted), 'kept': sorted(kept)}


def run(runtime, workdir, rto_target=60, rpo_target=86400, retention_days=30):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    alerts = alert_delivery(workdir / 'alerts', {'id': 'drill-1', 'level': 'warning',
                                                 'text': 'local drill alert'})
    audit = workdir / 'audit.jsonl'
    audit.write_text('{"t": 1, "e": "A"}\n{"t": 2, "e": "B"}\n')
    anchor = offsite_anchor(audit, workdir / 'worm', workdir / 'offsite')
    retention_dir = workdir / 'retention'
    retention_dir.mkdir(exist_ok=True)
    old = retention_dir / 'old.log'
    old.write_text('old')
    os.utime(old, (time.time() - (retention_days + 1) * 86400,) * 2)
    (retention_dir / 'new.log').write_text('new')
    kept = retention(retention_dir, retention_days)
    drill = dr_drill.run(runtime, workdir / 'dr')
    rto_ok = drill['rto_seconds'] <= rto_target if rto_target else True
    rpo = drill['recovery_point_seconds']
    rpo_ok = (rpo is not None and rpo <= rpo_target) if rpo_target else True
    checks = {'alert_delivered': alerts['delivered'],
              'offsite_ok': anchor['offsite_ok_after_loss'],
              'retention_deleted_old': 'old.log' in kept['deleted'],
              'retention_kept_new': 'new.log' in kept['kept'],
              'dr_no_invalid': not drill['invalid_databases'],
              'dr_complete': not drill['missing_authority_databases'] and drill['backup_coverage_complete'],
              'rto_within_target': rto_ok, 'rpo_within_target': rpo_ok}
    return {'checks': checks, 'ok': all(checks.values()),
            'alert': alerts, 'anchor': anchor, 'retention': kept,
            'dr': {'rto_seconds': drill['rto_seconds'], 'recovery_point_seconds': rpo,
                   'invalid_databases': drill['invalid_databases'],
                   'missing_authority_databases': drill['missing_authority_databases']},
            'targets': {'rto': rto_target, 'rpo': rpo_target, 'retention_days': retention_days},
            'note': 'local operations drill; real pager delivery, WORM and off-site DR are external'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--workdir', type=Path, required=True)
    parser.add_argument('--rto-target', type=float, default=60)
    parser.add_argument('--rpo-target', type=float, default=86400)
    parser.add_argument('--retention-days', type=int, default=30)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = run(args.runtime, args.workdir, args.rto_target, args.rpo_target, args.retention_days)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + '\n')
    print(text)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
