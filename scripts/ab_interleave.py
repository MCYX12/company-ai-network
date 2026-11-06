"""Randomised old/new interleaved driver: experiment binding, per-submission gate, chain reconcile.

Manifest v3 binds static conditions + per-side code/runtime. Every run_one() (including the second
side of a pair) is preceded by an in-flight gate that returns clear/inflight/unknown and pauses on
anything but clear. Execution success requires leaf state=succeeded AND query_code=OK AND
result_code=OK; a latest observation that is refused/unknown never falls back to an old success.
Observations persist to a private 0600 JSONL bound to the manifest; the public report is projected
and gate-checked while the raw report goes to a private 0600 file.
"""
import argparse
import hashlib
import json
import os
import random
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bench_business
from scripts.archive_evidence import _json_values, assert_public, private_target

MANIFEST_VERSION=3
TERMINAL={'succeeded','failed','expired','cancelled'}
REQUIRED_META=('model','material')


def _git_commit(root):
    try:
        result=subprocess.run(['git','-C',str(root),'rev-parse','--short','HEAD'],
                              capture_output=True,text=True,timeout=10)
        return result.stdout.strip() or None
    except (OSError,subprocess.SubprocessError):
        return None


def _tree_digest(path):
    path=Path(path);digest=hashlib.sha256()
    if not path.exists():
        return None
    for file in sorted(path.rglob('*.py')):
        digest.update(str(file.relative_to(path)).encode());digest.update(file.read_bytes())
    return digest.hexdigest()[:16]


def manifest_of(descriptor):
    return hashlib.sha256(json.dumps(descriptor,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]


def load_records(path):
    path=Path(path)
    if not path.exists():
        return 'missing',None,[]
    lines=[line for line in path.read_text(errors='replace').splitlines() if line.strip()]
    if not lines:
        return 'empty',None,[]
    try:
        header=json.loads(lines[0])
    except ValueError:
        return 'corrupt',None,[]
    if not isinstance(header,dict) or 'manifest' not in header:
        return 'corrupt',None,[]
    if header.get('manifest_version')!=MANIFEST_VERSION:
        return 'unsupported',header,[]
    rows=[]
    for index,line in enumerate(lines[1:],start=2):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if index==len(lines):
                return 'trailing_partial',header,rows
            return 'corrupt',header,rows
    return 'valid',header,rows


def validate_records(header,rows,samples,meta,expected):
    ids=[sample['id'] for sample in samples]
    if len(set(ids))!=len(ids):
        raise SystemExit('DUPLICATE_SAMPLE_ID')
    missing=[key for key in REQUIRED_META if not meta.get(key)]
    if missing:
        raise SystemExit('META_REQUIRED_KEYS %s'%missing)
    seen={}
    for row in rows:
        key=(row.get('side'),row.get('sample'),row.get('repeat'))
        if key not in expected:
            raise SystemExit('RECORD_OUTSIDE_BATCH %s'%(key,))
        if row.get('attempt_id')!='%s|%s|%s'%(key[0],key[1],key[2]):
            raise SystemExit('ATTEMPT_ID_MISMATCH %s'%(row.get('attempt_id'),))
        signature=(row.get('task_id'),row.get('state'),row.get('wall_seconds'),row.get('error'))
        if key in seen and seen[key]!=signature:
            raise SystemExit('CONFLICTING_RECORDS %s'%(key,))
        seen[key]=signature
    return seen


def private_path(raw):
    """Outside the repo, directory 0700, file created 0600; never chmod a shared parent."""
    path=Path(raw).expanduser()
    repo=Path(os.path.realpath(ROOT));resolved=Path(os.path.realpath(path))
    if resolved==repo or str(resolved).startswith(str(repo)+os.sep):
        raise SystemExit('refusing to write raw data inside the repository: %s'%resolved)
    parent_existed=path.parent.exists()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not parent_existed:
        os.chmod(path.parent,0o700)
    if path.exists():
        os.chmod(path,0o600)
    return path


def append_private(path,values):
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(descriptor,'a') as stream:
        for value in values:
            stream.write(json.dumps(value,ensure_ascii=False)+'\n')
    os.chmod(path,0o600)


def _eventless(stdout):
    values=[value for value in _json_values(stdout)
            if isinstance(value,dict) and 'event' not in value]
    return values[-1] if values else None


def _submitted_task_id(stdout):
    for value in _json_values(stdout):
        if isinstance(value,dict) and value.get('event')=='submitted':
            return value.get('task_id')
    return None


def run_one(side,root,runtime,url,identity,target,question,sample,repeat,order,
            client_wait,task_ttl,timeout):
    attempt_id='%s|%s|%d'%(side,sample['id'],repeat)
    env={**os.environ,'POC_RUNTIME':str(runtime),'NATS_URL':url,
         'AGENT_WAIT_SECONDS':str(client_wait),'AGENT_TASK_TTL_SECONDS':str(task_ttl)}
    started=time.monotonic();timed_out=False;stderr=''
    try:
        proc=subprocess.run([sys.executable,'-m','app.peer_client','--identity',identity,
                             '--target',target,'--question-stdin','--wait'],
                            cwd=str(root),env=env,input=question,text=True,
                            capture_output=True,timeout=timeout)
        stdout,stderr,rc=proc.stdout,proc.stderr,proc.returncode
    except subprocess.TimeoutExpired as expired:
        stdout=expired.stdout if isinstance(expired.stdout,str) else (expired.stdout or b'').decode('utf-8','replace')
        stderr=expired.stderr if isinstance(expired.stderr,str) else (expired.stderr or b'').decode('utf-8','replace')
        rc=None;timed_out=True
    wall=time.monotonic()-started
    extracted=bench_business._extract(_eventless(stdout))
    total=(extracted.get('timings') or {}).get('total_seconds') if isinstance(extracted.get('timings'),dict) else None
    task_id=extracted.get('task_id') or _submitted_task_id(stdout)
    attempt=dict(extracted)
    attempt.update({'attempt_id':attempt_id,'side':side,'category':sample['category'],
                    'sample':sample['id'],'repeat':repeat,'order':order,
                    'wall_seconds':round(wall,3),'returncode':rc,'timed_out':timed_out,
                    'task_id':task_id,'submission_unknown':bool(task_id is None and timed_out),
                    'error':None if not timed_out else 'TimeoutExpired',
                    'client_overhead_seconds':(round(max(0.0,wall-total),3)
                                               if isinstance(total,(int,float)) else None),
                    'success':bool(not timed_out and rc==0 and extracted['query_code']=='OK'
                                   and extracted['state']=='succeeded' and extracted['result_code']=='OK'),
                    'stderr_tail':(stderr or '')[-200:] if extracted['state'] is None else None})
    return attempt


def query_task(root,runtime,url,identity,target,task_id,timeout=60):
    env={**os.environ,'POC_RUNTIME':str(runtime),'NATS_URL':url}
    proc=subprocess.run([sys.executable,'-m','app.peer_client','--identity',identity,
                         '--target',target,'--task-id',task_id],
                        cwd=str(root),env=env,capture_output=True,text=True,timeout=timeout)
    return bench_business._extract(_eventless(proc.stdout))


def resolve_chain(root,runtime,url,identity,target,task_id,max_hops=4):
    chain=[];seen=set();current=task_id;limit=False
    while current and current not in seen and len(chain)<=max_hops:
        seen.add(current)
        observed=query_task(root,runtime,url,identity,target,current)
        chain.append({'task_id':current,'state':observed.get('state'),
                      'query_code':observed.get('query_code'),'result_code':observed.get('result_code')})
        if observed.get('state') in ('failed','expired') and observed.get('retry_task_id'):
            current=observed['retry_task_id'];continue
        break
    else:
        limit=True
    leaf=chain[-1] if chain else {'task_id':task_id,'state':None,'query_code':None,'result_code':None}
    return {'chain':chain,'leaf':leaf,'cycle_or_hop_limit':bool(limit),
            'resolved':(not limit and leaf.get('state') in TERMINAL)}


def observe_side_digest(root,runtime,url,identity,target,timeout=20):
    probe=("import asyncio,json,sys\n"
           "from app.common import connect,request,query\n"
           "async def m():\n"
           "    nc=await connect(sys.argv[1],timeout=6)\n"
           "    b=request(sys.argv[1]);b.pop('target_agent_id');b['action']='list_agents';b['payload']={}\n"
           "    r=await query(nc,sys.argv[1],b)\n"
           "    row=next((a for a in r['data']['agents'] if a['agent_id']==sys.argv[2]),None)\n"
           "    digest=((row or {}).get('build') or {}).get('running_digest')\n"
           "    print(json.dumps({'code':r.get('code'),'digest':digest,'agent':sys.argv[2]}))\n"
           "    await nc.close()\n"
           "asyncio.run(m())\n")
    try:
        proc=subprocess.run([sys.executable,'-c',probe,identity,target],cwd=str(root),
                            env={**os.environ,'POC_RUNTIME':str(runtime),'NATS_URL':url},
                            capture_output=True,text=True,timeout=timeout)
        if proc.returncode!=0:
            return {'digest':'unknown','source':'probe-failed','detail':'rc=%s'%proc.returncode}
        for value in reversed(_json_values(proc.stdout)):
            if isinstance(value,dict) and 'digest' in value:
                if value.get('code')!='OK':
                    return {'digest':'unknown','source':'probe-code','detail':str(value.get('code'))}
                digest=value.get('digest')
                if isinstance(digest,str) and len(digest)==16 and all(c in '0123456789abcdef' for c in digest):
                    return {'digest':digest,'source':'observed'}
                return {'digest':'unknown','source':'unknown-digest','detail':str(digest)[:24]}
        return {'digest':'unknown','source':'no-structured-event'}
    except (OSError,subprocess.SubprocessError):
        return {'digest':'unknown','source':'probe-error'}


def wait_ready(root,runtime,url,identity,target,timeout=60):
    probe=("import asyncio,sys\n"
           "from app.common import connect,request,query\n"
           "async def m():\n"
           "    nc=await connect(sys.argv[1],timeout=6)\n"
           "    b=request(sys.argv[1]);b.pop('target_agent_id');b['action']='list_agents';b['payload']={}\n"
           "    r=await query(nc,sys.argv[1],b)\n"
           "    ok=any(a['agent_id']==sys.argv[2] and a.get('presence')=='online'\n"
           "           and 'ask_peer' in (a.get('available_task_actions') or []) for a in r['data']['agents'])\n"
           "    await nc.close();print('YES' if ok else 'NO')\n"
           "asyncio.run(m())\n")
    env_base={**os.environ,'POC_RUNTIME':str(runtime),'NATS_URL':url}
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        proc=subprocess.run([sys.executable,'-c',probe,identity,target],cwd=str(root),env=env_base,
                            capture_output=True,text=True,timeout=15)
        if 'YES' in proc.stdout:
            return True
        time.sleep(1.5)
    return False


def inflight_status(runtime,task_ids):
    """clear | inflight | unknown; unknown (missing/unreadable ledger) must pause, not pass."""
    ids=[value for value in task_ids if value]
    path=Path(runtime)/'tasks.sqlite3'
    if not path.exists():
        return {'status':'unknown','tasks':[],'reason':'LEDGER_MISSING'}
    if not ids:
        return {'status':'clear','tasks':[],'reason':None}
    try:
        db=sqlite3.connect('file:%s?mode=ro'%path,uri=True)
        marks=','.join('?'*len(TERMINAL));slots=','.join('?'*len(ids))
        rows=[row[0] for row in db.execute('select task_id from tasks where state not in (%s) and task_id in (%s)'
                                           %(marks,slots),list(TERMINAL)+ids)]
        db.close()
        return ({'status':'inflight','tasks':rows,'reason':None} if rows
                else {'status':'clear','tasks':[],'reason':None})
    except sqlite3.Error as error:
        return {'status':'unknown','tasks':[],'reason':'LEDGER_%s'%type(error).__name__}


def read_observations(path,manifest):
    path=Path(path)
    if not path.exists():
        return []
    observations=[]
    for line in path.read_text(errors='replace').splitlines():
        line=line.strip()
        if not line:
            continue
        try:
            value=json.loads(line)
        except ValueError:
            continue
        if value.get('manifest')==manifest:
            observations.append(value)
    return observations


def observation_state(observation):
    """None-safe: a missing observation returns None instead of raising."""
    if not observation:
        return None
    leaf=observation.get('leaf') or {}
    return leaf.get('state') if observation.get('resolved') else None


def effective_state(row,observation):
    """Terminal only when the latest observation (or the record) is resolved and terminal.

    A failed original whose chain leaf is still running yields 'unresolved', which blocks the
    next submission instead of being treated as a settled failure.
    """
    if observation is not None:
        state=observation_state(observation)
        if not observation.get('resolved'):
            return state or 'unresolved'
        return state or 'unresolved'
    # No observation: a failed/expired record with a recovery link has an UNKNOWN child state.
    if row.get('state') in ('failed','expired') and row.get('retry_task_id'):
        return 'unresolved'
    return row.get('state')


def submission_gate(runtime,done_rows,observations=None):
    """Block the next submission unless every prior attempt is settled.

    A prior non-terminal attempt with no task id is an unconfirmed submission. Even when the
    ledger looks clear, an unsettled record means the previous attempt was not confirmed settled.
    """
    latest={observation['attempt_id']:observation for observation in (observations or [])}
    prior=[]
    for row in done_rows:
        key=row.get('attempt_id') or '%s|%s|%s'%(row.get('side'),row.get('sample'),row.get('repeat'))
        if effective_state(row,latest.get(key)) not in TERMINAL:
            prior.append(row)
    if not prior:
        return {'status':'clear','tasks':[],'reason':None}
    if any(not row.get('task_id') for row in prior):
        return {'status':'unknown_submission','tasks':[],'reason':'NO_TASK_ID'}
    status=inflight_status(runtime,known_task_ids(prior,observations))
    if status['status']=='clear':
        return {'status':'unsettled_record','tasks':[],'reason':'RECORD_NOT_SETTLED'}
    return status


def known_task_ids(rows,observations=None):
    ids=set()
    for row in rows:
        for key in ('task_id','retry_task_id'):
            if row.get(key):
                ids.add(row[key])
    for observation in (observations or []):
        for hop in (observation.get('chain') or []):
            if hop.get('task_id'):
                ids.add(hop['task_id'])
    return sorted(ids)


def summarize_attempts(rows,observations=None,expected=None):
    by_key={}
    for row in rows:
        key=row.get('attempt_id') or '%s|%s|%s'%(row.get('side'),row.get('sample'),row.get('repeat'))
        by_key.setdefault(key,row)
    latest={obs['attempt_id']:obs for obs in (observations or [])}
    window_success=0;settled=0;reconciled_success=0
    original_state={};current_state={};unsettled=[]
    for key,row in by_key.items():
        if row.get('success'):
            window_success+=1
        original_state[key]=row.get('state')
        observation=latest.get(key)
        if observation is not None:
            leaf=observation.get('leaf') or {}
            state=leaf.get('state')
            query_code=leaf.get('query_code')
            result_code=leaf.get('result_code')
            resolved=bool(observation.get('resolved'))
        else:
            state=row.get('state');query_code=row.get('query_code');result_code=row.get('result_code')
            resolved=state in TERMINAL
        current_state[key]=state
        if state in TERMINAL:
            settled+=1
        else:
            unsettled.append(key)
        if resolved and state=='succeeded' and query_code=='OK' and result_code=='OK':
            reconciled_success+=1
    executed=len(by_key)
    execution_pass=bool(executed and reconciled_success==executed
                        and (expected is None or set(by_key)==set(expected)))
    return {'attempts_executed':executed,
            'client_window_success_rate':(round(window_success/executed,4) if executed else None),
            'reconciled_success_rate':(round(reconciled_success/executed,4) if executed else None),
            'terminal_coverage':(round(settled/executed,4) if executed else None),
            'results_settled':bool(executed and not unsettled),
            'execution_pass':execution_pass,
            'acceptance_scope':'execution-terminal only; not formal quality/performance acceptance',
            'original_execution_state':original_state,'current_retrievable_state':current_state,
            'unsettled':sorted(unsettled)}


def unique_rows(rows):
    by_key={}
    for row in rows:
        by_key.setdefault(row.get('attempt_id') or '%s|%s|%s'%(row.get('side'),row.get('sample'),row.get('repeat')),row)
    return list(by_key.values())


def write_private_report(directory,payload):
    private=private_target(directory)
    target=private/('ab-raw-%s-%s.json'%(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()),
                                          secrets.token_hex(2)))
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:
        stream.write(json.dumps(payload,ensure_ascii=False,indent=2))
    return target


def public_report(report):
    projected={'manifest':report['manifest'],'manifest_version':MANIFEST_VERSION,
               'seed':report['seed'],'orders':report['orders'],'meta':report.get('meta'),
               'stats':report['summary'],'compare':report['compare'],
               'expected_runs':report['expected_runs'],'completed_runs':report['completed_runs'],
               'attempts_executed':report['attempts_executed'],
               'client_window_success_rate':report['client_window_success_rate'],
               'reconciled_success_rate':report['reconciled_success_rate'],
               'terminal_coverage':report['terminal_coverage'],
               'results_settled':report['results_settled'],'execution_pass':report['execution_pass'],
               'acceptance_scope':report['acceptance_scope'],'missing':report['missing'],
               'sides':{side:{'git':info.get('git'),'source_sha256':info.get('source_sha256'),
                              'observed_digest':info.get('observed_digest'),
                              'digest_source':info.get('digest_source')}
                        for side,info in report['sides'].items()}}
    assert_public(projected)
    return projected


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples',required=True)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--seed',type=int,default=int(os.environ.get('AB_SEED','1')))
    parser.add_argument('--old-root',required=True);parser.add_argument('--new-root',required=True)
    parser.add_argument('--old-runtime',required=True);parser.add_argument('--new-runtime',required=True)
    parser.add_argument('--old-url',required=True);parser.add_argument('--new-url',required=True)
    parser.add_argument('--identity',default='ag-b');parser.add_argument('--target',default='ag-a')
    parser.add_argument('--client-wait',type=float,default=615)
    parser.add_argument('--task-ttl',type=int,default=600)
    parser.add_argument('--timeout',type=float,default=0)
    parser.add_argument('--warmup',default='none');parser.add_argument('--cache',default='cold')
    parser.add_argument('--max-pairs',type=int,default=0)
    parser.add_argument('--max-seconds',type=float,default=0)
    parser.add_argument('--records',default=str(Path.home()/'.local/share/company-agent-private/ab-interleave.jsonl'))
    parser.add_argument('--observations',default='')
    parser.add_argument('--out',required=True,help='PUBLIC report (no questions or paths)')
    parser.add_argument('--private-out',default='')
    parser.add_argument('--meta',action='append',default=[])
    parser.add_argument('--reconcile',action='store_true')
    parser.add_argument('--repair-trailing',action='store_true')
    args=parser.parse_args()
    timeout=args.timeout or (args.client_wait+60)
    samples=json.loads(Path(args.samples).read_text())
    meta=bench_business.parse_meta(args.meta)
    common={'samples':samples,'meta':meta,'seed':args.seed,'task_ttl':args.task_ttl,
            'client_wait':args.client_wait,'timeout':timeout,'identity':args.identity,
            'target':args.target,'prompt_template':'peer_client ask_peer','output':'text',
            'warmup':args.warmup,'cache':args.cache}
    execution={'A':{'root':args.old_root,'runtime':args.old_runtime,'url':args.old_url},
               'B':{'root':args.new_root,'runtime':args.new_runtime,'url':args.new_url}}
    sides={}
    for side,spec in execution.items():
        observation=observe_side_digest(spec['root'],spec['runtime'],spec['url'],args.identity,args.target)
        sides[side]={'root':str(Path(spec['root']).resolve()),'git':_git_commit(spec['root']),
                     'source_sha256':_tree_digest(Path(spec['root'])/'app'),
                     'runtime':str(spec['runtime']),'url':spec['url'],
                     'observed_digest':observation['digest'],'digest_source':observation['source']}
    static_sides={side:{key:value for key,value in info.items()
                        if key not in ('observed_digest','digest_source')} for side,info in sides.items()}
    descriptor={'version':MANIFEST_VERSION,'common':common,'sides':sides}
    manifest=manifest_of({'version':MANIFEST_VERSION,'common':common,'sides':static_sides})
    records=private_path(args.records)
    state,header,rows=load_records(records)
    if state in ('empty','corrupt','unsupported'):
        raise SystemExit('RECORDS_%s: refusing to continue; keep the file and use a new --records path'%state.upper())
    if state=='trailing_partial':
        if not args.repair_trailing:
            raise SystemExit('RECORDS_TRAILING_PARTIAL: last line is incomplete; rerun with --repair-trailing')
        lines=[line for line in records.read_text(errors='replace').splitlines() if line.strip()]
        records.write_text('\n'.join(lines[:-1])+'\n')
        print(json.dumps({'repaired_trailing_line':True}))
        state,header,rows=load_records(records)
    if state=='missing':
        descriptor_fd=os.open(records,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor_fd,'w') as stream:
            stream.write(json.dumps({'manifest':manifest,'manifest_version':MANIFEST_VERSION,
                                     'descriptor':descriptor})+'\n')
    elif header.get('manifest')!=manifest:
        raise SystemExit('EXPERIMENT_MISMATCH: use a new --records path (held %s, this is %s)'
                         %(header.get('manifest'),manifest))
    else:
        stored=(header.get('descriptor') or {}).get('sides') or {}
        for side,info in sides.items():
            before=(stored.get(side) or {}).get('observed_digest');now=info.get('observed_digest')
            if before and now and before!='unknown' and now!='unknown' and before!=now:
                raise SystemExit('RUNTIME_OBSERVATION_MISMATCH %s: %s != %s'%(side,before,now))
    expected={'%s|%s|%d'%(side,sample['id'],repeat) for side in ('A','B')
              for sample in samples for repeat in range(args.repeats)}
    validate_records(header,rows,samples,meta,{(k.split('|')[0],k.split('|')[1],int(k.split('|')[2]))
                                               for k in expected})
    observations=read_observations(private_path(args.observations or (records.parent/('observations-%s.jsonl'%records.stem))),manifest)
    observations_path=private_path(args.observations or (records.parent/('observations-%s.jsonl'%records.stem)))
    if args.reconcile:
        new_observations=[]
        for row in rows:
            if not row.get('task_id') or row.get('submission_unknown'):
                continue
            spec=execution[row['side']]
            resolved=resolve_chain(spec['root'],spec['runtime'],spec['url'],args.identity,args.target,row['task_id'])
            new_observations.append({'attempt_id':row.get('attempt_id'),'original_task_id':row['task_id'],
                                     'observed_at':time.time(),'event_id':secrets.token_hex(4),
                                     'manifest':manifest,**resolved})
        append_private(observations_path,new_observations)
        observations=observations+new_observations
        settled=summarize_attempts(unique_rows(rows),observations,expected)
        raw={'manifest':manifest,'descriptor':descriptor,'observations':observations,
             'summary':settled,'records':str(records),'observations_file':str(observations_path),
             'seed':args.seed,'orders':{},'expected_runs':len(expected),
             'completed_runs':len([key for key in expected if key in {r.get('attempt_id') for r in rows}]),
             'missing':sorted(expected-{r.get('attempt_id') for r in rows})[:50],
             'meta':meta,'summary':{'old':{},'new':{}},'compare':None,
             'attempts_executed':settled['attempts_executed'],
             'client_window_success_rate':settled['client_window_success_rate'],
             'reconciled_success_rate':settled['reconciled_success_rate'],
             'terminal_coverage':settled['terminal_coverage'],
             'results_settled':settled['results_settled'],'execution_pass':settled['execution_pass'],
             'acceptance_scope':settled['acceptance_scope'],'sides':sides}
        Path(args.out).write_text(json.dumps(public_report(raw),ensure_ascii=False,indent=2))
        if args.private_out:
            print(json.dumps({'private':str(write_private_report(args.private_out,raw))}))
        print(json.dumps({'reconciled':len(new_observations),'history_observations':len(observations)-len(new_observations),**settled},ensure_ascii=False))
        return
    for spec in execution.values():
        if not wait_ready(spec['root'],spec['runtime'],spec['url'],args.identity,args.target):
            raise SystemExit('target not ready for one side')
    done={} if not rows else {row.get('attempt_id') or '%s|%s|%s'%(row.get('side'),row.get('sample'),row.get('repeat')):row
                              for row in rows}
    plan=[(sample,repeat) for sample in samples for repeat in range(args.repeats)]
    random.Random(args.seed).shuffle(plan)
    orders={};pairs_run=0;started=time.monotonic();paused=None
    with records.open('a') as stream:
        for sample,repeat in plan:
            if args.max_pairs and pairs_run>=args.max_pairs:
                break
            if args.max_seconds and time.monotonic()-started>=args.max_seconds:
                break
            first,second=random.Random('%d|%s|%d'%(args.seed,sample['id'],repeat)).choice([('A','B'),('B','A')])
            orders['%s#%d'%(sample['id'],repeat)]=first+second
            ran_any=False
            for side in (first,second):
                key='%s|%s|%d'%(side,sample['id'],repeat)
                if key in done:
                    continue
                # Per-submission gate: the second side of a pair is gated too. A reconciled
                # leaf counts as settled so a resumed batch can proceed without a false pause.
                latest_obs={observation['attempt_id']:observation for observation in observations}
                effective=[{**row,'state':(observation_state(latest_obs.get(key)) or row.get('state'))}
                           for key,row in done.items()]
                gate=submission_gate(execution[side]['runtime'],effective,observations)
                if gate['status']!='clear':
                    paused={'status':gate['status'],'tasks':gate['tasks'],'reason':gate['reason'],
                            'known':known_task_ids(done.values(),observations)}
                    print(json.dumps({'inflight_paused':paused,
                                      'hint':'retrieve before starting another attempt; tasks are not cancelled'},
                                     ensure_ascii=False))
                    break
                spec=execution[side]
                attempt=run_one(side,spec['root'],spec['runtime'],spec['url'],args.identity,
                                args.target,sample['question'],sample,repeat,first+second,
                                args.client_wait,args.task_ttl,timeout)
                stream.write(json.dumps(attempt,ensure_ascii=False)+'\n');stream.flush()
                done[key]=attempt;ran_any=True
            if paused:
                break
            if ran_any:
                pairs_run+=1
    state,header,rows_all=load_records(records)
    rows_unique=unique_rows(rows_all)
    by_side={'old':[row for row in rows_unique if row.get('side')=='A'],
             'new':[row for row in rows_unique if row.get('side')=='B']}
    reports={'samples':samples,'meta':meta,'target':args.target}
    summary={name:bench_business.summarize(side_rows) for name,side_rows in by_side.items()}
    compare=None
    if by_side['old'] and by_side['new']:
        compare=bench_business.compare_reports({**reports,'summary':summary['old']},
                                               {**reports,'summary':summary['new']})
    settled=summarize_attempts(rows_unique,observations,expected)
    report={'manifest':manifest,'descriptor':descriptor,'seed':args.seed,'orders':orders,
            'samples':samples,'meta':meta,'target':args.target,'summary':summary,'compare':compare,
            'expected_runs':len(expected),'completed_runs':len(set(done)&expected),
            'missing':sorted(expected-set(done))[:50],'pairs_run':pairs_run,'records':str(records),
            'complete':not (expected-set(done)),'paused_inflight':paused,'sides':sides,
            'observations_file':str(observations_path),**settled}
    if args.private_out:
        print(json.dumps({'private':str(write_private_report(args.private_out,report))}))
    Path(args.out).write_text(json.dumps(public_report(report),ensure_ascii=False,indent=2))
    print(json.dumps({'out':args.out,'manifest':manifest,'completed':report['completed_runs'],
                      'expected':report['expected_runs'],'complete':report['complete'],
                      'client_window_success_rate':settled['client_window_success_rate'],
                      'reconciled_success_rate':settled['reconciled_success_rate'],
                      'results_settled':settled['results_settled'],
                      'execution_pass':settled['execution_pass']},ensure_ascii=False))


if __name__=='__main__':
    raise SystemExit(main())
