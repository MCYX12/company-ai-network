"""Authenticated administrator channel: mTLS admin identity -> Router -> operations.

The Router accepts admin_* actions only from identities listed in
<RUNTIME>/admin-identities.json, and every action is appended to
<RUNTIME>/admin-history/channel.jsonl. The channel replaces ad-hoc SSH for routine
issuance/revocation/DLQ/backup/offboarding. It never exposes the offline root.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .common import RUNTIME

ID = re.compile(r'^[a-z][a-z0-9-]{0,63}$')
KINDS = {'client', 'server', 'ocsp'}
OCSP_URL = 'http://127.0.0.1:2560'


def admins(runtime=None):
    """Authorized mTLS admin identities: the web account library (bound identities) plus the
    backward-compatible admin-identities.json file."""
    runtime = Path(runtime) if runtime else Path(RUNTIME)
    names = set()
    path = runtime / 'admin-identities.json'
    if path.exists():
        try:
            data = json.loads(path.read_text())
            names |= {name for name in data.get('admins', []) if isinstance(name, str)}
        except (OSError, ValueError):
            pass
    database = runtime / 'accounts.sqlite3'
    if database.exists():
        try:
            from .accounts import Accounts
            names |= Accounts(database).admin_identities()
        except Exception:
            pass
    return {name for name in names if ID.fullmatch(name)}


def _ca_dir(runtime):
    marker = runtime / 'certs/ENTERPRISE_CA.json'
    if marker.exists():
        return Path(json.loads(marker.read_text())['ca_dir'])
    return runtime / 'enterprise-ca'


def _audit(runtime, actor, action, correlation, summary):
    folder = runtime / 'admin-history'
    folder.mkdir(parents=True, exist_ok=True)
    entry = {'time': datetime.now(timezone.utc).isoformat(), 'actor': actor, 'action': action,
             'correlation_id': correlation, 'summary': summary}
    with (folder / 'channel.jsonl').open('a') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')


def handle(action, payload, actor, correlation, runtime=None):
    runtime = Path(runtime) if runtime else Path(RUNTIME)
    payload = payload or {}
    result = _dispatch(action, payload, runtime)
    _audit(runtime, actor, action, correlation, result.get('summary', {}) if isinstance(result, dict) else {})
    return result


def _dispatch(action, payload, runtime):
    from scripts import ca_tool
    from .task_store import TaskStore
    ca_dir = _ca_dir(runtime)
    if action == 'admin_status':
        return {'runtime': str(runtime), 'ca_dir': str(ca_dir), 'admins': sorted(admins(runtime)),
                'summary': {'admins': len(admins(runtime))}}
    if action == 'admin_ca_status':
        return {'status': ca_tool.status(ca_dir), 'root': ca_tool.root_status(ca_dir),
                'summary': {'issued': len(ca_tool.status(ca_dir)['issued'])}}
    if action == 'admin_issue':
        name = payload.get('name'); kind = payload.get('kind', 'client'); days = payload.get('days', 365)
        if not isinstance(name, str) or not ID.fullmatch(name) or kind not in KINDS or type(days) != int or not 1 <= days <= 3650:
            raise ValueError('INVALID_REQUEST')
        issued = ca_tool.issue(ca_dir, name, kind, days, OCSP_URL)
        return {'name': name, 'kind': kind, 'days': days,
                'cert': Path(issued['cert']).read_text(), 'key': Path(issued['key']).read_text(),
                'chain': Path(issued['chain']).read_text(),
                'summary': {'name': name, 'kind': kind, 'days': days}}
    if action == 'admin_renew':
        name = payload.get('name'); kind = payload.get('kind', 'client'); days = payload.get('days', 365)
        key = ca_dir / 'intermediate/private' / f'{name}.key'
        if not isinstance(name, str) or not ID.fullmatch(name) or kind not in KINDS or not key.exists():
            raise ValueError('INVALID_REQUEST')
        issued = ca_tool.renew(ca_dir, name, kind, key, days, OCSP_URL)
        return {'name': name, 'kind': kind,
                'cert': Path(issued['cert']).read_text(), 'chain': Path(issued['chain']).read_text(),
                'summary': {'name': name, 'kind': kind}}
    if action == 'admin_revoke':
        name = payload.get('name'); reason = payload.get('reason', 'cessationOfOperation')
        if not isinstance(name, str) or not ID.fullmatch(name) or not isinstance(reason, str) or not 1 <= len(reason) <= 64:
            raise ValueError('INVALID_REQUEST')
        ca_tool.revoke(ca_dir, name, reason)
        crl = ca_tool.crl(ca_dir)
        return {'revoked': name, 'reason': reason, 'crl': crl['crl'], 'summary': {'name': name, 'reason': reason}}
    if action == 'admin_dlq_list':
        limit = int(payload.get('limit', 100))
        if not 1 <= limit <= 1000:
            raise ValueError('INVALID_REQUEST')
        rows = TaskStore(runtime / 'tasks.sqlite3').dead_letters(limit)
        keys = ['task_id', 'owner', 'target', 'operation', 'attempts', 'created_at', 'deadline']
        return {'dead_letters': [{k: r[k] for k in keys} for r in rows], 'summary': {'count': len(rows)}}
    if action == 'admin_dlq_requeue':
        task_id = payload.get('task_id')
        if not isinstance(task_id, str):
            raise ValueError('INVALID_REQUEST')
        row = TaskStore(runtime / 'tasks.sqlite3').requeue(task_id, actor='admin-channel')
        return {'task_id': task_id, 'state': row['state'], 'summary': {'task_id': task_id}}
    if action == 'admin_backup':
        from scripts.runtime_backup import backup
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        output = runtime / 'admin-history' / f'backup-{stamp}'
        report = backup(runtime, output)
        return {'output': str(output), 'report': report, 'summary': {'output': str(output)}}
    if action == 'admin_offboard':
        from scripts.offboard_member import offboard
        employee = payload.get('employee'); reason = payload.get('reason'); apply = bool(payload.get('apply'))
        if not isinstance(employee, str) or not ID.fullmatch(employee) or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('INVALID_REQUEST')
        report = offboard(runtime, employee, reason, apply=apply)
        return {'report': report, 'summary': {'employee': employee, 'applied': report.get('applied', False)}}
    raise ValueError('UNSUPPORTED_ACTION')
