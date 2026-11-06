"""Disposable isolated stacks for A/B comparison and long-task maintenance drills.

Each stack has its own runtime (certs, registry, conversation grant), port and a stub
harness with a deterministic sleep, so effects can be measured without touching the
production `.runtime-lan`. Processes are registered as soon as they are spawned and
reclaimed on failure; stop verifies PID start time and command line before signalling.
"""
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
NATS_SERVER=ROOT/'.runtime/bin/nats-server'
_PROTECTED=[ROOT/'.runtime', ROOT/'.runtime-lan']


def _resolve(runtime):
    return Path(runtime).resolve()


def _refuse_production(runtime):
    for protected in _PROTECTED:
        try:
            protected_resolved=protected.resolve()
        except OSError:
            continue
        if runtime==protected_resolved or str(runtime).startswith(str(protected_resolved)+os.sep):
            raise SystemExit('refusing to operate on production runtime: %s' % runtime)


def _proc_info(pid):
    try:
        stat=Path('/proc/%d/stat' % pid).read_text()
        tail=stat[stat.rfind(')')+2:].split()
        cmdline=[p.decode('utf-8','replace') for p in Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0') if p]
        return {'starttime':tail[19], 'cmdline':cmdline}
    except (OSError,IndexError):
        return None


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _owned(info):
    current=_proc_info(info['pid'])
    return bool(current and current.get('starttime')==info.get('starttime')
                and current.get('cmdline')==info.get('cmd'))


def _load(path):
    return json.loads(Path(path).read_text())


def setup(args):
    runtime=_resolve(args.runtime)
    _refuse_production(runtime)
    if runtime.exists():
        raise SystemExit('runtime already exists: %s' % runtime)
    os.umask(0o077)
    sys.path.insert(0, str(ROOT/'scripts'))
    sys.path.insert(0, str(ROOT))
    import bootstrap
    bootstrap.RUNTIME=runtime
    bootstrap.main(listen=args.port)
    registry={'schema_version': 1, 'agents': [
        {'agent_id': 'ag-a', 'device_id': 'iso', 'employee_id': 'iso-a',
         'tool': 'dsh-harness', 'projects': ['demo-project'], 'enabled': True,
         'capabilities': ['get_status', 'ask_peer']},
        {'agent_id': 'ag-b', 'device_id': 'iso', 'employee_id': 'iso-b',
         'tool': 'fixture', 'projects': ['demo-project'], 'enabled': True}]}
    (runtime/'registry.json').write_text(json.dumps(registry, ensure_ascii=False))
    (runtime/'harness.json').write_text(json.dumps({'tools': {'dsh-harness': {
        'command': ['/bin/sh', '-c', 'sleep %d; echo ISO-OK' % int(args.sleep)],
        'timeout_seconds': 540, 'cwd': str(runtime), 'env': {'PATH': '/usr/bin:/bin'}}}}))
    from app.conversation_access import AccessStore
    store=AccessStore(runtime/'conversation-access.sqlite3')
    for caller in ('ag-b', 'ag-a'):
        row=store.request(caller, 'ag-a', ['demo-project'])
        store.decide(row['approval_id'], 'approved', 0, 'iso setup')
    (runtime/'endpoint.txt').write_text('tls://127.0.0.1:%d\n' % args.port)
    print('iso runtime ready: %s (port %d, sleep %ds)' % (runtime, args.port, int(args.sleep)))


def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=.2):
            return True
    except OSError:
        return False


def _wait_log(log, needle, timeout, offset):
    deadline=time.time()+timeout
    while time.time()<deadline:
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(offset)
                if needle.encode() in stream.read():
                    return True
        time.sleep(.1)
    return False


def start(args):
    runtime=_resolve(args.runtime)
    _refuse_production(runtime)
    repo=Path(args.repo_root).resolve()
    assert (runtime/'nats.conf').exists(), 'run setup first'
    pidfile=runtime/'pids.json'
    if pidfile.exists():
        previous=_load(pidfile)
        if any(_pid_alive(info['pid']) for info in previous.values() if isinstance(info,dict)):
            raise SystemExit('stack already running; stop first')
        pidfile.unlink()
    endpoint=(runtime/'endpoint.txt').read_text().strip()
    port=int(endpoint.rsplit(':', 1)[1])
    if _port_open('127.0.0.1', port):
        raise SystemExit('port %d already in use' % port)
    logdir=runtime/'logs'; logdir.mkdir(exist_ok=True)
    env={**os.environ, 'POC_RUNTIME': str(runtime), 'NATS_URL': endpoint,
         'POC_JETSTREAM': '0', 'NATS_SERVER': str(NATS_SERVER), 'PYTHONUNBUFFERED': '1'}
    spawned={}
    offsets={}
    def spawn(name, argv):
        log=logdir/('%s.log' % name)
        offsets[name]=log.stat().st_size if log.exists() else 0
        stream=log.open('a')
        process=subprocess.Popen(argv, cwd=str(repo), env=env, stdout=stream,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        info=_proc_info(process.pid) or {}
        spawned[name]={'pid': process.pid, 'starttime': info.get('starttime'), 'cmd': list(argv)}
        return process
    def cleanup():
        for name in ('ag-a', 'router', 'nats'):
            info=spawned.get(name)
            if info:
                _terminate(info)
        try: pidfile.unlink()
        except FileNotFoundError: pass
    try:
        spawn('nats', [str(NATS_SERVER), '-c', str(runtime/'nats.conf')])
        for _ in range(100):
            if _port_open('127.0.0.1', port):
                break
            time.sleep(.1)
        else:
            raise SystemExit('nats did not open port %d' % port)
        spawn('router', [sys.executable, '-u', '-m', 'app.router'])
        spawn('ag-a', [sys.executable, '-u', '-m', 'app.agent', 'ag-a'])
        if not _wait_log(logdir/'router.log', 'router ready', 20, offsets['router']):
            raise SystemExit('router not ready; see %s' % (logdir/'router.log'))
        if not _wait_log(logdir/'ag-a.log', 'ag-a ready', 20, offsets['ag-a']):
            raise SystemExit('ag-a not ready; see %s' % (logdir/'ag-a.log'))
        pidfile.write_text(json.dumps(spawned, indent=2))
    except BaseException:
        cleanup()
        raise
    print('started', json.dumps({name: info['pid'] for name, info in spawned.items()}))


def _terminate(info):
    if not _owned(info):
        return False
    try:
        os.kill(info['pid'], signal.SIGTERM)
    except OSError:
        return False
    deadline=time.time()+3
    while time.time()<deadline and _pid_alive(info['pid']):
        time.sleep(.1)
    if _pid_alive(info['pid']):
        try:
            os.kill(info['pid'], signal.SIGKILL)
        except OSError:
            pass
    return True


def stop(args):
    runtime=_resolve(args.runtime)
    _refuse_production(runtime)
    pidfile=runtime/'pids.json'
    if not pidfile.exists():
        print('no pids.json; nothing to stop')
        return
    pids=_load(pidfile)
    stopped=[];skipped=[]
    for name in ('ag-a', 'router', 'nats'):
        info=pids.get(name)
        if not info:
            continue
        if _owned(info):
            stopped.append(name)
        else:
            skipped.append(name)
    for name in stopped:
        _terminate(pids[name])
    pidfile.unlink()
    print('stopped %s skipped %s' % (stopped, skipped))
    if skipped:
        print('WARNING: %s could not be confirmed as belonging to this stack; not sent signals' % skipped)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command', required=True)
    setup_p=sub.add_parser('setup')
    setup_p.add_argument('--runtime', required=True)
    setup_p.add_argument('--port', type=int, required=True)
    setup_p.add_argument('--sleep', type=int, default=30)
    setup_p.set_defaults(func=setup)
    start_p=sub.add_parser('start')
    start_p.add_argument('--runtime', required=True)
    start_p.add_argument('--repo-root', default=str(ROOT))
    start_p.set_defaults(func=start)
    stop_p=sub.add_parser('stop')
    stop_p.add_argument('--runtime', required=True)
    stop_p.set_defaults(func=stop)
    args=parser.parse_args()
    return args.func(args)


if __name__=='__main__':
    raise SystemExit(main())
