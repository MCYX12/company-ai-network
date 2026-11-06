"""Report project summary freshness; --mark writes a refresh-due sidecar for ops/timers."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import project_state


def scan(runtime):
    return project_state.statuses(runtime)


def mark(runtime, rows):
    folder = Path(runtime) / 'project-records'
    for row in rows:
        sidecar = folder / (row['project'] + '.refresh.json')
        sidecar.write_text(json.dumps({'refresh_due': row['refresh_due'], 'freshness': row['freshness'],
                                       'snapshot_at': row['snapshot_at'],
                                       'checked_at': datetime.now(timezone.utc).isoformat()},
                                      ensure_ascii=False, indent=2))
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--mark', action='store_true', help='write <project>.refresh.json sidecars')
    args = parser.parse_args()
    rows = scan(args.runtime)
    if args.mark:
        mark(args.runtime, rows)
    print(json.dumps({'projects': rows, 'refresh_due': sum(1 for r in rows if r['refresh_due'])}, ensure_ascii=False, indent=2))
    return 1 if any(r['refresh_due'] for r in rows) else 0


if __name__ == '__main__':
    raise SystemExit(main())
