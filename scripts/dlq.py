"""Manual dead-letter queue operations: list, replay one task, purge old entries, quota."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.task_store import TaskStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    requeue = sub.add_parser('requeue'); requeue.add_argument('--task-id', required=True)
    quota = sub.add_parser('quota'); quota.add_argument('--limit-mb', type=int); quota.add_argument('--enforce', action='store_true')
    purge = sub.add_parser('purge'); purge.add_argument('--older-than-days', type=float, required=True); purge.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    store = TaskStore(args.runtime / 'tasks.sqlite3')
    if args.command == 'list':
        rows = store.dead_letters()
        print(json.dumps([{k: r[k] for k in ['task_id', 'owner', 'target', 'operation', 'attempts', 'created_at', 'deadline']} for r in rows], ensure_ascii=False, indent=2))
    elif args.command == 'requeue':
        print(json.dumps(store.public(store.requeue(args.task_id)), ensure_ascii=False))
    elif args.command == 'quota':
        limit = args.limit_mb * 1048576 if args.limit_mb else None
        report = store.enforce_quota(limit) if args.enforce else store.quota_status(limit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.command == 'purge':
        cutoff = time.time() - args.older_than_days * 86400
        if args.apply:
            print(json.dumps({'purged': store.purge_dead_letters(cutoff)}, ensure_ascii=False))
        else:
            older = [r for r in store.dead_letters(100000) if r['created_at'] < cutoff]
            print(json.dumps({'would_purge': len(older)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
