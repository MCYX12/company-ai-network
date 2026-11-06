"""Run only NATS, Router and ag-a on Ubuntu. ag-b must run on the Mac."""
import os
from pathlib import Path
import signal
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT/'.runtime-lan'
os.environ['POC_RUNTIME'] = str(RUNTIME)
os.environ['NATS_URL'] = (RUNTIME/'endpoint.txt').read_text().strip()
os.environ['NATS_SERVER'] = str(ROOT/'.runtime/bin/nats-server')
# Deployed default: durable dispatch through JetStream (rendered nats.conf enables it).
# Set POC_JETSTREAM=0 to force Core NATS.
os.environ.setdefault('POC_JETSTREAM', '1')
# Enterprise admission policy: managed employees are explicit in the roster, so an unmanaged
# ('unknown') identity must not be admitted. Employees are seeded by the admin/HR sync.
os.environ.setdefault('POC_IAM_STRICT', '1')
os.environ.setdefault('POC_SHARED_REPLAY', '1')
sys.path.insert(0, str(ROOT))
from scripts.run_local import Stack
from urllib.parse import urlparse

endpoint = urlparse(os.environ['NATS_URL'])
stack = Stack(RUNTIME)
def stop(*_):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, stop)
try:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((endpoint.hostname, endpoint.port))
    stack.server()
    for _ in range(50):
        if stack.children['nats'].poll() is not None:
            raise RuntimeError('NATS failed; inspect .runtime-lan/nats.log')
        try:
            with socket.create_connection((endpoint.hostname, endpoint.port), timeout=.1):
                break
        except OSError:
            time.sleep(.1)
    else:
        raise TimeoutError('NATS startup timed out')
    for name, args in [('router', ['app.router']), ('ag-a', ['app.agent', 'ag-a']),
                       ('ubuntu-harness-01', ['app.agent', 'ubuntu-harness-01'])]:
        stack.start(name, [sys.executable, '-u', '-m', *args])
    # A 5s readiness window was too tight: on a busy host the Router/Agents can need
    # longer, and the failure surfaced only as a bare "readiness timed out" after
    # systemd had already burned start-limit attempts. Allow 15s (POC_READY_TICKS) and
    # name the process that stalled so the cause is visible immediately.
    waiting = ['router', 'ag-a', 'ubuntu-harness-01']
    for _ in range(int(os.environ.get('POC_READY_TICKS', '150'))):
        dead = [name for name, proc in stack.children.items() if proc.poll() is not None]
        if dead:
            raise RuntimeError('Process failed: ' + ', '.join(sorted(dead)) + '; inspect .runtime-lan/*.log')
        waiting = [n for n in ('router', 'ag-a', 'ubuntu-harness-01')
                   if b'ready' not in (RUNTIME/f'{n}.log').read_bytes()[stack.offsets[n]:]]
        if not waiting:
            break
        time.sleep(.1)
    else:
        raise TimeoutError('Router/Agent readiness timed out, still not ready: ' + ', '.join(waiting)
                           + '; inspect .runtime-lan/*.log and check the NATS ACLs')
    print('Ubuntu ready: '+os.environ['NATS_URL']+'; ag-a and ubuntu-harness-01 running; Mac agents connect from the Mac.', flush=True)
    while all(p.poll() is None for p in stack.children.values()):
        time.sleep(1)
    raise RuntimeError('A stack process exited unexpectedly; supervisor must restart the whole stack')
except KeyboardInterrupt:
    pass
finally:
    stack.close()
