"""Alerting over the health snapshot and the task ledger (assessment: monitoring/alerts).

Thresholds come from the site profile 'alerts' section; alerts are appended to
<runtime>/health/alerts.jsonl so existing tailing keeps working.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULTS = {'queue_seconds_max': 60.0, 'run_seconds_max': 600.0, 'auth_denied_total': 50,
            'project_age_seconds_max': 2592000, 'renewal_failures': 0, 'disk_free_gib_min': 2.0,
            'dead_letter_max': 0}


def thresholds(runtime=None):
    merged = dict(DEFAULTS)
    try:
        from app import site
        merged.update(site.load(runtime).get('alerts', {}))
    except Exception:
        pass
    return merged


def _check(health, name):
    for item in (health or {}).get('checks', []):
        if item.get('check') == name:
            return item
    return None


def values(runtime, health):
    runtime = Path(runtime)
    data = {'queue_seconds_max': 0.0, 'run_seconds_max': 0.0, 'auth_denied_total': 0,
            'project_age_seconds_max': 0, 'renewal_failures': 0, 'disk_free_gib': None,
            'dead_letter': 0, 'dead_letter_by_target': {}, 'authorized_directory': 'unknown'}
    db = runtime / 'tasks.sqlite3'
    if db.exists():
        connection = sqlite3.connect(db)
        try:
            queue, run = [], []
            for row in connection.execute('SELECT DISTINCT task_id FROM task_audit'):
                events = {}
                for event in connection.execute('SELECT event, timestamp FROM task_audit WHERE task_id=? ORDER BY timestamp', (row[0],)):
                    events.setdefault(event['event'] if isinstance(event, sqlite3.Row) else event[0], event[1])
                if 'accepted' in events and 'dispatched' in events:
                    queue.append(max(0.0, events['dispatched'] - events['accepted']))
                if 'dispatched' in events:
                    for terminal in ('succeeded', 'failed', 'expired', 'cancelled'):
                        if terminal in events and events[terminal] >= events['dispatched']:
                            run.append(events[terminal] - events['dispatched'])
                            break
            data['queue_seconds_max'] = max(queue) if queue else 0.0
            data['run_seconds_max'] = max(run) if run else 0.0
            data['auth_denied_total'] = connection.execute(
                "SELECT count(*) FROM task_audit WHERE event='FORBIDDEN'").fetchone()[0]
        finally:
            connection.close()
    records = runtime / 'project-records'
    if records.is_dir():
        for record in records.glob('*.json'):
            try:
                stamp = json.loads(record.read_text()).get('updated_at')
                parsed = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                data['project_age_seconds_max'] = max(data['project_age_seconds_max'],
                                                      int(time.time() - parsed.timestamp()))
            except (OSError, ValueError, TypeError):
                continue
    disk = _check(health, 'disk_space')
    if disk and isinstance(disk.get('detail'), dict):
        data['disk_free_gib'] = disk['detail'].get('free_gib')
    ledger = _check(health, 'task_ledger')
    if ledger and isinstance(ledger.get('detail'), dict):
        detail = ledger['detail']
        # Prefer the 24h window: the lifetime counter never decreases, so a threshold of 0 on it
        # would keep alerting forever after the first delivery failure. Older health payloads
        # (and the packaged device worker) still only carry the lifetime key.
        data['dead_letter'] = detail.get('dead_letter_delivery_failed_24h',
                                         detail.get('dead_letter_delivery_failed', 0))
        # Per-target breakdown of the same window, for diagnosis (no separate rule).
        data['dead_letter_by_target'] = detail.get('dead_letter_by_target_24h') or {}
    directory = _check(health, 'authorized_directory')
    if directory:
        data['authorized_directory'] = directory.get('status')
    if isinstance((health or {}).get('renewal'), dict):
        data['renewal_failures'] = int(health['renewal'].get('failures', 0))
    return data


def evaluate(runtime, health=None, limits=None):
    runtime = Path(runtime)
    if health is None:
        path = runtime / 'health/latest.json'
        health = json.loads(path.read_text()) if path.exists() else {}
    limits = limits or thresholds(runtime)
    measured = values(runtime, health)
    handlers = limits.get('handlers', {}) if isinstance(limits.get('handlers'), dict) else {}
    alerts = []
    def check(rule, value, limit, severity='alert'):
        if value is None:
            return
        if value > limit:
            alerts.append({'rule': rule, 'value': value, 'threshold': limit, 'severity': severity,
                           'owner': handlers.get(rule, '')})
    check('queue_seconds_max', measured['queue_seconds_max'], limits['queue_seconds_max'], 'warn')
    check('run_seconds_max', measured['run_seconds_max'], limits['run_seconds_max'], 'warn')
    check('auth_denied_total', measured['auth_denied_total'], limits['auth_denied_total'], 'warn')
    check('project_age_seconds_max', measured['project_age_seconds_max'], limits['project_age_seconds_max'], 'warn')
    check('renewal_failures', measured['renewal_failures'], limits['renewal_failures'])
    check('dead_letter', measured['dead_letter'], limits['dead_letter_max'])
    if measured['disk_free_gib'] is not None and measured['disk_free_gib'] < limits['disk_free_gib_min']:
        alerts.append({'rule': 'disk_free_gib', 'value': measured['disk_free_gib'],
                       'threshold': limits['disk_free_gib_min'], 'severity': 'alert',
                       'owner': handlers.get('disk_free_gib', '')})
    if measured['authorized_directory'] not in ('ok', 'unknown'):
        alerts.append({'rule': 'authorized_directory', 'value': measured['authorized_directory'],
                       'threshold': 'ok', 'severity': 'alert',
                       'owner': handlers.get('authorized_directory', '')})
    return {'measured': measured, 'thresholds': limits, 'alerts': alerts,
            'status': 'ok' if not alerts else 'alert'}


def notify(report, webhook=None, command=None, timeout=10):
    """Deliver an alert report to a real channel: HTTP webhook and/or a shell command."""
    payload = json.dumps(report, ensure_ascii=False).encode()
    delivered = {'webhook': None, 'command': None}
    if webhook:
        import urllib.request
        request = urllib.request.Request(webhook, data=payload, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                delivered['webhook'] = response.status
        except OSError as error:
            delivered['webhook'] = 'error: ' + str(error)
    if command:
        import subprocess
        process = subprocess.run(command, shell=True, input=payload, capture_output=True)
        delivered['command'] = process.returncode
    return delivered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--health', type=Path)
    parser.add_argument('--out', type=Path, help='default <runtime>/health/alerts.jsonl')
    parser.add_argument('--webhook', help='POST the report JSON here (e.g. a Feishu/Slack webhook)')
    parser.add_argument('--command', help='run this shell command with the report JSON on stdin (e.g. sendmail)')
    parser.add_argument('--notify-always', action='store_true', help='notify even when there are no alerts')
    args = parser.parse_args()
    health = json.loads(args.health.read_text()) if args.health and args.health.exists() else None
    report = evaluate(args.runtime, health)
    report['evaluated_at'] = datetime.now(timezone.utc).isoformat()
    out = args.out or (args.runtime / 'health/alerts.jsonl')
    if report['alerts']:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('a') as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + '\n')
    if report['alerts'] or args.notify_always:
        if args.webhook or args.command:
            report['notified'] = notify(report, args.webhook, args.command)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report['alerts'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
