"""Administrator CLI over the mTLS admin channel. Run on the administrator computer."""
import argparse
import asyncio
import json

from .common import connect, query, request

ACTIONS = ['admin_status', 'admin_ca_status', 'admin_issue', 'admin_renew', 'admin_revoke',
           'admin_dlq_list', 'admin_dlq_requeue', 'admin_backup', 'admin_offboard']


async def main(args):
    async def quiet(*_):
        pass
    nc = await connect(args.identity, error_cb=quiet, disconnected_cb=quiet)
    try:
        body = request(args.identity)
        body.pop('target_agent_id', None)
        body['action'] = args.action
        body['payload'] = json.loads(args.payload) if args.payload else {}
        result = await query(nc, args.identity, body, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('code') == 'OK' else 1
    finally:
        await nc.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--identity', default='admin-01')
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--payload', help='JSON payload, e.g. {"name":"laptop-01","kind":"client","days":365}')
    parser.add_argument('--timeout', type=int, default=60)
    argv = parser.parse_args()
    raise SystemExit(asyncio.run(main(argv)))
