"""Administrator CLI for the local employment roster (enterprise IAM placeholder)."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.iam import RosterStore, provider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', type=Path, help='roster sqlite (defaults to runtime)')
    sub = parser.add_subparsers(dest='command', required=True)
    setp = sub.add_parser('set')
    setp.add_argument('--employee', required=True)
    setp.add_argument('--status', required=True, choices=['active', 'suspended', 'left'])
    sub.add_parser('list')
    statusp = sub.add_parser('status')
    statusp.add_argument('--employee', required=True)
    args = parser.parse_args()
    store = RosterStore(args.path)
    if args.command == 'set':
        store.set(args.employee, args.status)
        print(json.dumps({'employee_id': args.employee, 'status': args.status}, ensure_ascii=False))
    elif args.command == 'list':
        print(json.dumps(store.listing(), ensure_ascii=False, indent=2))
    elif args.command == 'status':
        print(json.dumps({'employee_id': args.employee, 'status': provider().status(args.employee)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
