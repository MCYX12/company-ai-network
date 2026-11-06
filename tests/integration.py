"""Real NATS and separate router/agent processes, including negative ACL cases."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import nats
from app.common import PREFIX, RUNTIME, connect, query, request, encode
from scripts.run_local import Stack

async def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence = ROOT/'evidence'/stamp
    evidence.mkdir(parents=True)
    stack = Stack(evidence)
    records = []
    clients = []
    def record(name, detail=None):
        records.append(dict(test=name, status='PASS', detail=detail))
        print(f'PASS {name}', flush=True)
    async def reject_connection(name, **options):
        errors = []
        async def error_cb(error):
            errors.append(str(error))
        try:
            nc = await asyncio.wait_for(nats.connect(servers=['tls://localhost:14222'],
                allow_reconnect=False, connect_timeout=1, error_cb=error_cb, **options), 4)
        except (nats.errors.Error, ssl.SSLError, OSError, asyncio.TimeoutError):
            record(name, errors[-1:] or ['connection rejected'])
        else:
            await nc.close()
            raise AssertionError(f'{name}: unauthorized connection succeeded')
    try:
        stack.boot()
        a = await connect('ag-a')
        clients.append(a)
        result = await query(a, 'ag-a', request())
        assert result['code'] == 'OK' and result['data']['synthetic'] is True
        assert result['data']['agent_id'] == 'ag-b'
        record('authorized A -> Router -> B -> A query', result)

        bad = request(); bad['sender_agent_id'] = 'ag-b'
        assert (await query(a, 'ag-a', bad))['code'] == 'FORBIDDEN'
        record('forged sender rejected')
        c = await connect('ag-c'); clients.append(c)
        assert (await query(c, 'ag-c', request('ag-c')))['code'] == 'FORBIDDEN'
        record('cross-project policy denied')
        bad = request(); bad['role'] = 'admin'
        assert (await query(a, 'ag-a', bad))['code'] == 'INVALID_REQUEST'
        record('untrusted role field rejected')
        bad = request(); bad['action'] = 'execute_shell'
        assert (await query(a, 'ag-a', bad))['code'] == 'INVALID_REQUEST'
        record('non-whitelisted action rejected')
        bad = request(); bad['expires_at'] = time.time()-1
        assert (await query(a, 'ag-a', bad))['code'] == 'EXPIRED'
        record('expired request rejected')
        body = request()
        assert (await query(a, 'ag-a', body))['code'] == 'OK'
        assert (await query(a, 'ag-a', body))['code'] == 'REPLAY_REJECTED'
        record('replayed message rejected')

        errors = asyncio.Queue()
        async def error_cb(error):
            await errors.put(str(error))
        probe = await connect('ag-a', error_cb=error_cb); clients.append(probe)
        for subject in [f'{PREFIX}.ingress.ag-b.request', f'{PREFIX}.agent.ag-b.query', '$JS.API.INFO']:
            await probe.publish(subject, b'{}')
            await probe.flush()
            error = await asyncio.wait_for(errors.get(), 2)
            assert 'permissions violation' in error.lower()
            record('publish denied: '+subject)
        for subject in [f'{PREFIX}.client.ag-b.response', '_INBOX.>', '$SYS.>']:
            sub = await probe.subscribe(subject)
            await probe.flush()
            error = await asyncio.wait_for(errors.get(), 2)
            assert 'permissions violation' in error.lower()
            await sub.unsubscribe()
            record('subscribe denied: '+subject)

        context = ssl.create_default_context(cafile=str(RUNTIME/'certs/ca.crt'))
        await reject_connection('missing client certificate rejected', tls=context)
        context = ssl.create_default_context(cafile=str(RUNTIME/'certs/ca.crt'))
        context.load_cert_chain(RUNTIME/'certs/unregistered.crt', RUNTIME/'certs/unregistered.key')
        await reject_connection('valid CA but unregistered identity rejected', tls=context)
        context = ssl.create_default_context()
        context.load_cert_chain(RUNTIME/'certs/ag-a.crt', RUNTIME/'certs/ag-a.key')
        await reject_connection('untrusted server CA rejected', tls=context)

        # Router must survive malformed JSON and remain able to process valid traffic.
        await a.publish(f'{PREFIX}.ingress.ag-a.request', b'not-json')
        await a.flush()
        assert (await query(a, 'ag-a', request()))['code'] == 'OK'
        record('malformed payload does not kill router')
        body = request(target='ag-a'); body['expires_at'] = time.time()+.5
        assert (await query(a, 'ag-a', body))['code'] == 'TIMEOUT'
        record('unavailable target times out')

        # Actual broker restart; retain existing agent/router/client processes.
        reconnected = asyncio.Event()
        async def reconnect_cb():
            reconnected.set()
        resilient = await connect('ag-a', reconnected_cb=reconnect_cb); clients.append(resilient)
        stack.stop_child('nats')
        await asyncio.sleep(1.2)
        start = time.monotonic()
        stack.server()
        await asyncio.wait_for(reconnected.wait(), 10)
        for attempt in range(5):
            body = request(); body['expires_at'] = time.time()+1
            result = await query(resilient, 'ag-a', body, timeout=2)
            if result['code'] == 'OK':
                break
            await asyncio.sleep(.5)
        assert result['code'] == 'OK'
        record('broker restart and existing clients recover', {'recovery_seconds': round(time.monotonic()-start, 3)})
    except BaseException as error:
        records.append(dict(test='suite', status='FAIL', detail=repr(error)))
        raise
    finally:
        for nc in clients:
            await nc.close()
        stack.close()
        summary = dict(scope='local P1 only, synthetic data', results=records,
                       passed=sum(r['status']=='PASS' for r in records),
                       failed=sum(r['status']=='FAIL' for r in records),
                       limitations=['No real VLAN test', 'No macOS/Windows execution',
                                    'No IAM/JWT, durable tasks, HA or production acceptance'])
        (evidence/'results.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print('Evidence:', evidence)

if __name__ == '__main__':
    asyncio.run(main())
