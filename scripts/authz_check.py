"""Explain one authorization decision through the unified entry."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.authz import KINDS, decide


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--actor', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--kind', choices=list(KINDS), required=True)
    parser.add_argument('--project')
    parser.add_argument('--projects', help='comma-separated')
    parser.add_argument('--runtime', type=Path)
    args = parser.parse_args()
    projects = args.projects.split(',') if args.projects else None
    decision = decide(args.actor, args.target, args.kind, project=args.project, projects=projects, runtime=args.runtime)
    print(json.dumps(decision, ensure_ascii=False))
    return 0 if decision.get('allowed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
