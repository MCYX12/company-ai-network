import asyncio, json, subprocess, sys, time
sys.path.insert(0, '.')
from app.common import connect, query, request

WANT = ['mac-claude-code-01', 'mac-codex-01', 'mac-hermes-01']
out = open(sys.argv[1], 'w')

async def ne(e): pass
async def nd(): pass

async def main():
    nc = await connect('ag-a', error_cb=ne, disconnected_cb=nd)
    try:
        for _ in range(16):
            try:
                b = request('ag-a'); b.pop('target_agent_id'); b['action'] = 'list_agents'; b['payload'] = {}
                r = await query(nc, 'ag-a', b, timeout=8)
                online = sorted(a['agent_id'] for a in r['data']['agents'] if a['presence'] == 'online')
            except Exception as e:
                online = ['error:' + type(e).__name__]
            src = subprocess.run("ss -tn 2>/dev/null | awk '$4 ~ /:14222$/ {print $5}' | cut -d: -f1 | sort | uniq -c | tr '\\n' ' '",
                                 shell=True, capture_output=True, text=True).stdout.strip()
            mac = [x for x in WANT if x in online]
            out.write(json.dumps({'t': round(time.time(), 1), 'mac_online': mac, 'n_mac': len(mac), 'src': src}) + chr(10))
            out.flush()
            await asyncio.sleep(5)
    finally:
        await nc.close()

asyncio.run(main())
