#!/usr/bin/env python
"""Operator CLI for a confirmable maintenance drain.

    begin  -> raise the marker; workers advertise draining and stop taking new tasks
    run    -> poll until every target confirmed draining and all in-flight tasks settled
    finish -> remove the marker (workers resume) after the service has been stopped
    cancel -> abort maintenance and let workers resume without stopping anything

The default on timeout is to defer maintenance; only --force interrupts a running task,
and the recorded reason/affected tasks are kept for audit. --stop is the single place that
touches systemd, and it refuses to run unless the run is settled (or explicitly forced).
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from app import maintenance
from app.common import connect,request,query,parse_servers
from nats.errors import Error as NatsError


def draining_targets(identity,endpoint=None):
    async def probe():
        nc=await connect(identity,timeout=8,**({'servers':parse_servers(endpoint)} if endpoint else {}))
        try:
            body=request(identity);body.pop('target_agent_id',None)
            body['action']='list_agents';body['payload']={}
            response=await query(nc,identity,body)
            if response.get('code')!='OK':
                return []
            return [a['agent_id'] for a in response['data']['agents'] if a.get('readiness')=='draining']
        finally:
            await nc.close()
    return asyncio.run(probe())


def local_workers(runtime=None):
    """(agent identities for THIS runtime, probe_failed) from the process table.

    Bound to the runtime's POC_RUNTIME so an isolated stack is not counted, and an
    unreadable /proc entry is reported as a failed probe instead of an empty (covered) set.
    """
    runtime_path=None
    if runtime:
        try: runtime_path=str(Path(runtime).resolve())
        except OSError: runtime_path=str(runtime)
    found=set();failed=False
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            parts=[p.decode('utf-8','replace') for p in (entry/'cmdline').read_bytes().split(b'\0') if p]
        except OSError:
            continue
        agent_id=None
        for index,part in enumerate(parts):
            if part.endswith('app.agent') and index+1<len(parts):
                agent_id=parts[index+1];break
        if not agent_id:
            continue
        if runtime_path:
            try:
                environ=(entry/'environ').read_bytes().split(b'\0')
            except OSError:
                failed=True;continue
            values={}
            for item in environ:
                if b'=' in item:
                    key,_,value=item.partition(b'=')
                    values[key.decode('utf-8','replace')]=value.decode('utf-8','replace')
            if values.get('POC_RUNTIME')!=runtime_path:
                continue
        found.add(agent_id)
    return found,failed


def targets_cover(local,targets):
    return set(local)<=set(targets)


def stop_unit(unit):
    result=subprocess.run(['systemctl','--user','stop',unit],capture_output=True,text=True)
    return result.returncode, (result.stderr or result.stdout).strip()


def cmd_begin(args):
    state=maintenance.begin(args.targets.split(','),max_wait=args.max_wait,reason=args.reason)
    print(json.dumps(state,ensure_ascii=False,indent=2))
    return 0


def cmd_status(args):
    print(json.dumps(maintenance.status(),ensure_ascii=False,indent=2))
    return 0


def cmd_force(args):
    state=maintenance.mark_forced(maintenance.read())
    print(json.dumps(state,ensure_ascii=False,indent=2))
    return 0


def cmd_cancel(args):
    print(json.dumps(maintenance.cancel(),ensure_ascii=False,indent=2))
    return 0


def cmd_finish(args):
    print(json.dumps(maintenance.finish(),ensure_ascii=False,indent=2))
    return 0


def cmd_run(args):
    state=maintenance.read()
    if state is None:
        print(json.dumps({'error':'NO_MAINTENANCE','hint':'run begin first'},ensure_ascii=False))
        return 2
    deadline=time.monotonic()+max(1,int(state.get('max_wait',maintenance.DEFAULT_MAX_WAIT)))
    run_id=state.get('id')
    def ended(current):
        if not isinstance(current,dict) or current.get('id')!=run_id or current.get('state') in {'cancelled','finished','done'}:
            print(json.dumps({'maintenance_run_ended':True,'state':current.get('state') if isinstance(current,dict) else 'missing'},ensure_ascii=False))
            return True
        return False
    local,probe_failed=local_workers(args.runtime)
    def stop_guard():
        discovered,failed=local_workers(args.runtime)
        missing=sorted(discovered-set(state['targets']))
        unknown=state.get('worker_unknown') or {}
        if (missing or failed or probe_failed or unknown) and not args.allow_partial_targets:
            print(json.dumps({'error':'WORKER_COVERAGE_UNCONFIRMED','missing':missing,
                              'probe_failed':bool(failed or probe_failed),'unknown':unknown,
                              'hint':'the whole stack would be stopped but local worker coverage '
                                     'or drain state is not fully confirmed; fix it or pass '
                                     '--allow-partial-targets'},ensure_ascii=False))
            return False
        return True
    while time.monotonic()<deadline:
        try:confirmed=draining_targets(args.identity,getattr(args,'endpoint',None))
        except (OSError,ValueError,asyncio.TimeoutError,NatsError) as error:
            current=maintenance.read()
            if ended(current):return 0
            state=maintenance.mark_deferred(current,runtime=args.runtime,
                reason='MAINTENANCE_CONNECTION_FAILED',blocking=(state or {}).get('pending'),
                unknown=(state or {}).get('worker_unknown'))
            print(json.dumps({'deferred':True,'state':state['state'],'reason':state['deferred_reason'],
                'error_type':type(error).__name__,'hint':'Check --endpoint or NATS_URL and runtime certificates; '
                'retry run after fixing, or cancel to let workers resume. No service was stopped.'},ensure_ascii=False))
            return 3
        current=maintenance.read()
        if ended(current):return 0
        state=maintenance.evaluate(current,confirmed,
                                    runtime=args.runtime,local_targets=local)
        print(json.dumps({'state':state['state'],'confirmed':state.get('confirmed'),
                          'pending':state.get('pending'),
                          'worker_unknown':state.get('worker_unknown')},ensure_ascii=False),flush=True)
        if state['state']=='settled':
            if args.stop:
                if not stop_guard():
                    return 4
                code,detail=stop_unit(args.unit)
                print(json.dumps({'stopped':code==0,'unit':args.unit,'detail':detail},ensure_ascii=False))
                if code==0:
                    maintenance.finish()
                    return 0
                return code
            print('SETTLED: safe to stop the service',flush=True)
            return 0
        if state['state'] in {'deferred','forced'}:
            if state['state']=='deferred' and args.force:
                maintenance.mark_forced(state)
                continue
            if state['state']=='forced' and args.stop:
                if not stop_guard():
                    return 4
                code,detail=stop_unit(args.unit)
                print(json.dumps({'stopped':code==0,'unit':args.unit,'forced':True,'detail':detail},
                                 ensure_ascii=False))
                if code==0:
                    maintenance.finish()
                    return 0
                return code
            blocking=state.get('pending') or {}
            print(json.dumps({'deferred':True,'blocking':blocking,
                              'hint':'in-flight tasks remain; re-run later, or --force to interrupt'},
                             ensure_ascii=False))
            return 3
        time.sleep(max(0.5,args.interval))
    # Persist the timeout as an explicit deferred decision; never leave "command timed out"
    # while the stored state still reads as draining.
    current=maintenance.read()
    if ended(current):return 0
    current=maintenance.mark_deferred(current,runtime=args.runtime,
                                      reason='MAINTENANCE_WAIT_EXCEEDED',
                                      blocking=(current or {}).get('pending'),
                                      unknown=(current or {}).get('worker_unknown'))
    print(json.dumps({'deferred':True,'state':current['state'],
                      'reason':current.get('deferred_reason'),
                      'blocking':current.get('blocking'),
                      'hint':'in-flight tasks remain; re-run later, or --force to interrupt'},
                     ensure_ascii=False))
    return 3


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    begin=sub.add_parser('begin');begin.add_argument('--targets',required=True)
    begin.add_argument('--max-wait',type=int,default=maintenance.DEFAULT_MAX_WAIT)
    begin.add_argument('--reason',default='');begin.set_defaults(func=cmd_begin)
    sub.add_parser('status').set_defaults(func=cmd_status)
    run=sub.add_parser('run');run.add_argument('--identity',default='ag-a')
    run.add_argument('--runtime',default=os.environ.get('POC_RUNTIME') or str(ROOT/'.runtime-lan'))
    run.add_argument('--endpoint',help='Explicit TLS NATS endpoint; otherwise uses NATS_URL')
    run.add_argument('--interval',type=float,default=5)
    run.add_argument('--stop',action='store_true')
    run.add_argument('--force',action='store_true')
    run.add_argument('--unit',default='company-agent-stack.service')
    run.add_argument('--allow-partial-targets',action='store_true',
                     help='stop the whole stack even if some local workers are not in the targets')
    run.set_defaults(func=cmd_run)
    sub.add_parser('force').set_defaults(func=cmd_force)
    sub.add_parser('cancel').set_defaults(func=cmd_cancel)
    sub.add_parser('finish').set_defaults(func=cmd_finish)
    args=parser.parse_args()
    return args.func(args)


if __name__=='__main__':
    raise SystemExit(main())
