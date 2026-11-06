"""Run the read-only health check, persist its state, rotate logs and alert.

One periodic unit runs this on Ubuntu (systemd timer) and on macOS (launchd).
Exit code follows the health report: 0 ok, 1 fail, 2 warn, so the service manager
records a failure without hiding warnings. Alerts are appended to a local JSONL
and sent to the system logger; no network dependency.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.common import RUNTIME
from app.healthcheck import check
from scripts.rotate_logs import rotate


def _atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.health-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def run(identity, runtime=RUNTIME, max_bytes=5 * 1024 * 1024, keep=5):
    runtime = Path(runtime)
    state_dir = runtime / 'health'
    report = asyncio.run(check(identity, runtime))
    report['rotated_logs'] = rotate(runtime, max_bytes, keep)
    _atomic(state_dir / 'latest.json', json.dumps(report, ensure_ascii=False, indent=2))
    if report['status'] != 'ok':
        alert = {'time': datetime.now(timezone.utc).isoformat(), 'identity': identity, 'status': report['status'],
                 'problems': [c for c in report['checks'] if c['status'] in {'fail', 'warn'}]}
        with (state_dir / 'alerts.jsonl').open('a') as handle:
            handle.write(json.dumps(alert, ensure_ascii=False) + '\n')
        logger = shutil.which('logger')
        if logger:
            subprocess.run([logger, '-t', 'company-agent', f"health {report['status']} for {identity}"], check=False)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', required=True)
    parser.add_argument('--runtime', type=Path, default=RUNTIME)
    parser.add_argument('--max-bytes', type=int, default=5 * 1024 * 1024)
    parser.add_argument('--keep', type=int, default=5)
    args = parser.parse_args()
    report = run(args.identity, args.runtime, args.max_bytes, args.keep)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit({'ok': 0, 'fail': 1, 'warn': 2}[report['status']])


if __name__ == '__main__':
    main()
