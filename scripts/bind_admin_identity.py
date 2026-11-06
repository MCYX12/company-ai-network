"""Bind or clear the mTLS admin identity authorized by a web administrator account."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.accounts import Accounts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--admin', required=True)
    parser.add_argument('--identity', default='', help='empty clears the binding')
    args = parser.parse_args()
    store = Accounts(args.runtime / 'accounts.sqlite3')
    print(json.dumps(store.bind_identity('local-admin', args.admin, args.identity), ensure_ascii=False))


if __name__ == '__main__':
    main()
