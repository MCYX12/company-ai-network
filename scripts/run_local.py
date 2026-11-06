"""Foreground local stack; Ctrl-C stops only children created by this runner."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.common import RUNTIME

class Stack:
    def __init__(self, evidence=None, port=14222):
        self.children = {}
        self.logs = []
        self.offsets = {}
        self.port = port
        self.evidence = evidence or RUNTIME

    def start(self, name, command):
        path = self.evidence/f'{name}.log'
        self.offsets[name] = path.stat().st_size if path.exists() else 0
        log = path.open('a')
        self.logs.append(log)
        child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        self.children[name] = child
        return child

    def server(self):
        binary = Path(os.environ.get('NATS_SERVER', RUNTIME/'bin/nats-server'))
        return self.start('nats', [str(binary), '-c', str(RUNTIME/'nats.conf')])

    def stop_child(self, name):
        process = self.children.pop(name)
        if process.poll() is None:
            process.terminate()
            # A worker drains its in-flight answer on SIGTERM (POC_DRAIN_SECONDS), so the old
            # fixed 5s kill could cut off a question that was still running. Allow a bounded
            # grace before escalating; the systemd TimeoutStopSec is the outer backstop.
            grace = float(os.environ.get('POC_STOP_GRACE', '20'))
            try:
                process.wait(timeout=max(0.0, grace))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def close(self):
        for name in list(reversed(self.children)):
            self.stop_child(name)
        for log in self.logs:
            log.close()

    def boot(self):
        # Refuse to reuse an unrelated server listening on our port.
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', self.port))
        self.server()
        for _ in range(50):
            if self.children['nats'].poll() is not None:
                raise RuntimeError('NATS exited; inspect nats.log')
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise TimeoutError('NATS startup timed out')
        for name, module_args in [('router', ['app.router']), ('ag-b', ['app.agent', 'ag-b'])]:
            self.start(name, [sys.executable, '-u', '-m', *module_args])
        for _ in range(50):
            if all(b'ready' in (self.evidence/f'{name}.log').read_bytes()[self.offsets[name]:] for name in ['router', 'ag-b']):
                return
            if any(p.poll() is not None for p in self.children.values()):
                raise RuntimeError('Stack process exited; inspect logs')
            time.sleep(.1)
        raise TimeoutError('Stack startup timed out')

if __name__ == '__main__':
    # Deployed default: durable dispatch through JetStream (the rendered nats.conf
    # enables it). Set POC_JETSTREAM=0 to force Core NATS.
    os.environ.setdefault('POC_JETSTREAM', '1')
    port = int(os.environ.get('POC_PORT', '14222'))
    # Router/Agent subprocesses inherit this; keep them on the same port as the local NATS.
    os.environ.setdefault('NATS_URL', f'tls://localhost:{port}')
    stack = Stack(port=port)
    def shutdown(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    try:
        stack.boot()
        print(f'Local PoC ready: tls://localhost:{stack.port}. Ctrl-C stops this stack.', flush=True)
        while all(p.poll() is None for p in stack.children.values()):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stack.close()
