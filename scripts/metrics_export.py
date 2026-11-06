"""Export Prometheus text metrics from the health snapshot and the task ledger (section 13)."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


def _stat(lines, name, values):
    if values:
        lines.append('company_agent_%s_seconds_avg %.3f' % (name, sum(values) / len(values)))
        lines.append('company_agent_%s_seconds_max %.3f' % (name, max(values)))


def _age_seconds(updated_at):
    if not isinstance(updated_at, str):
        return None
    try:
        stamp = datetime.fromisoformat(updated_at.replace('Z', '+00:00'))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, time.time() - stamp.timestamp())


def pipeline_metrics(runtime, health=None):
    """Queue time, run time, task states, auth denials, project freshness, renewal failures."""
    runtime = Path(runtime)
    lines = []
    db = runtime / 'tasks.sqlite3'
    if db.exists():
        connection = sqlite3.connect(db)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute('SELECT state, count(*) AS n FROM tasks GROUP BY state ORDER BY state'):
                lines.append('company_agent_tasks_total{state="%s"} %d' % (row['state'], row['n']))
            queue, run = [], []
            for row in connection.execute('SELECT DISTINCT task_id FROM task_audit'):
                events = {}
                for event in connection.execute('SELECT event, timestamp FROM task_audit WHERE task_id=? ORDER BY timestamp',
                                                (row['task_id'],)):
                    events.setdefault(event['event'], event['timestamp'])
                if 'accepted' in events and 'dispatched' in events:
                    queue.append(max(0.0, events['dispatched'] - events['accepted']))
                if 'dispatched' in events:
                    for terminal in ('succeeded', 'failed', 'expired', 'cancelled'):
                        if terminal in events and events[terminal] >= events['dispatched']:
                            run.append(events[terminal] - events['dispatched'])
                            break
            _stat(lines, 'queue', queue)
            _stat(lines, 'run', run)
            denied = connection.execute("SELECT count(*) FROM task_audit WHERE event='FORBIDDEN'").fetchone()[0]
            lines.append('company_agent_auth_denied_total %d' % denied)
        finally:
            connection.close()
    records = runtime / 'project-records'
    if records.is_dir():
        for record in sorted(records.glob('*.json')):
            try:
                age = _age_seconds(json.loads(record.read_text()).get('updated_at'))
            except (OSError, ValueError):
                age = None
            if age is not None:
                lines.append('company_agent_project_age_seconds{project="%s"} %.0f' % (record.stem, age))
    if isinstance(health, dict) and isinstance(health.get('renewal'), dict) and 'failures' in health['renewal']:
        lines.append('company_agent_renewal_failures %d' % int(health['renewal']['failures']))
    return lines


def render(health, runtime=None):
    lines = ['# HELP company_agent_health_status 1 when status is ok, else 0',
             '# TYPE company_agent_health_status gauge',
             'company_agent_health_status ' + ('1' if health.get('status') == 'ok' else '0')]

    def emit(prefix, value):
        if isinstance(value, bool):
            lines.append(prefix + ' ' + ('1' if value else '0'))
        elif isinstance(value, (int, float)):
            lines.append(prefix + ' ' + str(value))
        elif isinstance(value, dict):
            for key, item in sorted(value.items()):
                emit(prefix + '_' + str(key), item)
        elif isinstance(value, list):
            lines.append(prefix + '_count ' + str(len(value)))

    for key, value in sorted(health.items()):
        if key == 'status':
            continue
        emit('company_agent_health_' + str(key), value)
    if runtime:
        lines.extend(pipeline_metrics(runtime, health))
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--health', type=Path, required=True)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    text = render(json.loads(args.health.read_text()), args.runtime)
    if args.output:
        args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
