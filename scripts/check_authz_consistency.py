"""Compare the authorization control plane two or more Routers would read.

    python -m scripts.check_authz_consistency --runtime /opt/company-agent/runtime \
        --runtime /mnt/shared/company-agent/runtime

Exit 0 when every runtime sees the same authorization/identity files, 1 on divergence,
2 when fewer than two runtimes are given. This is a comparison tool, not a fixer.
"""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import control_plane


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, action='append', required=True,
                        help='runtime directory to inspect (repeat for each Router)')
    parser.add_argument('--shared', action='store_true',
                        help='treat POC_AUTHZ_SHARED as declared (default reads the environment)')
    args = parser.parse_args()
    if len(args.runtime) < 2:
        print(json.dumps({'error': 'at least two --runtime arguments are required'}, ensure_ascii=False))
        return 2
    env = dict(os.environ)
    if args.shared:
        env['POC_AUTHZ_SHARED'] = '1'
    report = control_plane.compare(args.runtime, env=env)
    report['shared_runtime'] = env.get('POC_AUTHZ_RUNTIME')
    report['problems'] = [problem for runtime in args.runtime
                          for problem in control_plane.ha_problems(runtime, env=env)]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Missing authority files and HA authority declarations are gating failures too.
    return 0 if report['consistent'] and not report['problems'] and not report['missing'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
