"""Bundle machine-readable drill/deployment evidence into a public, minimal package.

Selects a set of tasks (expanded to their retry chains) instead of dumping the whole ledger.
The public package keeps only status, codes, timings and linkage: raw questions/answers are
never written to it. With --include-bodies the raw rows go to a private path, which is refused
inside the repository unless explicitly allowed.

    python -m scripts.archive_evidence --runtime .runtime-lan --task-id <id> \
        --out evidence/deploy-<stamp> --label deploy --client-out /tmp/client.out
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
SCHEMA_VERSION=2

PUBLIC_TASK_FIELDS=('task_id','owner','target','operation','state','created_at','deadline',
                    'attempts','cancel_state','retry_attempt')
PUBLIC_RETRY_FIELDS=('root_task_id','retry_task_id','attempt','reason','created')
PUBLIC_WORK_FIELDS=('task_id','state','acked','updated_at')
FORBIDDEN_KEYS={'payload','result','question','answer','text','content','summary','facts',
                'token','key','secret','password','credential','authorization','prompt'}


class PublicLeak(ValueError):
    pass


def violations(value,path=''):
    """Keys and strings that must never appear in a public evidence package."""
    found=[]
    if isinstance(value,dict):
        for key,item in value.items():
            here=('%s.%s'%(path,key)).lstrip('.')
            if str(key).lower() in FORBIDDEN_KEYS:
                found.append(here)
            found.extend(violations(item,here))
    elif isinstance(value,list):
        for index,item in enumerate(value):
            found.extend(violations(item,'%s[%d]'%(path,index)))
    elif isinstance(value,str) and (value.startswith('/Users/') or value.startswith('/home/')):
        found.append(path+':absolute-path')
    return found


def assert_public(bundle):
    found=sorted(set(violations(bundle)))
    if found:
        raise PublicLeak('public bundle contains non-public fields: '+', '.join(found[:8]))
    return True


def _table(db_path,table):
    if not Path(db_path).exists():
        return {'missing':True}
    try:
        db=sqlite3.connect('file:%s?mode=ro'%db_path,uri=True)
        db.row_factory=sqlite3.Row
        try:
            return [dict(row) for row in db.execute('select * from %s'%table)]
        finally:
            db.close()
    except sqlite3.Error as error:
        return {'error':type(error).__name__,'detail':str(error)[:200]}


def _rows(value):
    return value if isinstance(value,list) else []


def _expand(selected,retries):
    selected=set(selected)
    for _ in range(len(retries)+1):
        changed=False
        for row in retries:
            if row.get('root_task_id') in selected or row.get('retry_task_id') in selected:
                for key in ('root_task_id','retry_task_id'):
                    if row.get(key) and row[key] not in selected:
                        selected.add(row[key]);changed=True
        if not changed:
            break
    return selected


def _result_summary(row):
    code=None;timings={}
    try:
        result=json.loads(row.get('result') or '{}')
    except ValueError:
        code='UNPARSEABLE'
        result=None
    if isinstance(result,dict):
        code=result.get('code')
        data=result.get('data') if isinstance(result.get('data'),dict) else None
        if isinstance(data,dict) and isinstance(data.get('timings'),dict):
            timings=data['timings']
    return code,timings


def _json_values(text):
    values=[];index=0;decoder=json.JSONDecoder()
    while index<len(text):
        start=text.find('{',index)
        if start<0:
            break
        try:
            value,end=decoder.raw_decode(text[start:])
            values.append(value);index=start+end
        except ValueError:
            index=start+1
    return values


def _client(path):
    if not path or not Path(path).exists():
        return {'missing':True}
    values=_json_values(Path(path).read_text(errors='replace'))
    events=[v for v in values if isinstance(v,dict) and 'event' in v]
    terminal=None
    for value in reversed(values):
        if isinstance(value,dict) and 'event' not in value and isinstance(value.get('data'),dict):
            data=value['data']
            if 'state' in data:
                result=data.get('result') if isinstance(data.get('result'),dict) else {}
                terminal={'task_id':data.get('task_id'),'state':data.get('state'),
                          'retry_of':data.get('retry_of'),'result_code':result.get('code')}
                break
    return {'events':events,'terminal':terminal}


def _sha256(path):
    if not Path(path).exists():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]


def build(runtime,task_ids,client_out=None,client_rc=None,label='drill',
          build_supplied=None,build_observed=None):
    runtime=Path(runtime)
    tasks=_table(runtime/'tasks.sqlite3','tasks')
    retries=_table(runtime/'tasks.sqlite3','retries')
    missing=[]
    if isinstance(tasks,dict):missing.append('tasks:'+next(iter(tasks)))
    if isinstance(retries,dict):missing.append('retries:'+next(iter(retries)))
    retries_list=_rows(retries)
    selected=_expand(task_ids,retries_list)
    by_id={row['task_id']:row for row in _rows(tasks)}
    root_to_child={};child_to_root={};attempts={}
    for row in retries_list:
        if row.get('retry_task_id') in selected and row.get('root_task_id'):
            child_to_root[row['retry_task_id']]=row['root_task_id']
        if row.get('root_task_id') in selected and row.get('retry_task_id'):
            root_to_child[row['root_task_id']]=row['retry_task_id']
        attempts[row.get('retry_task_id')]=row.get('attempt')
    public_tasks=[]
    for task_id in sorted(selected):
        row=by_id.get(task_id)
        if row is None:
            missing.append('task:'+task_id);continue
        code,timings=_result_summary(row)
        record={key:row.get(key) for key in PUBLIC_TASK_FIELDS if key in row}
        record.update({'result_code':code,'timings':timings,
                       'retry_of':child_to_root.get(task_id),
                       'retry_task_id':root_to_child.get(task_id),
                       'retry_attempt':attempts.get(task_id)})
        public_tasks.append(record)
    public_retries=[{key:row.get(key) for key in PUBLIC_RETRY_FIELDS if key in row}
                    for row in retries_list
                    if row.get('root_task_id') in selected or row.get('retry_task_id') in selected]
    workers={}
    for db in sorted(runtime.glob('worker-*.sqlite3')):
        rows=_table(db,'work')
        if isinstance(rows,dict):
            workers[db.stem]=rows;missing.append(db.stem+':'+next(iter(rows)));continue
        workers[db.stem]=[{key:row.get(key) for key in PUBLIC_WORK_FIELDS if key in row}
                          for row in rows if row.get('task_id') in selected]
    maintenance=None
    if (runtime/'maintenance.json').exists():
        state=json.loads((runtime/'maintenance.json').read_text())
        maintenance={key:state.get(key) for key in
                     ('id','targets','state','confirmed','pending','worker_unknown','deferred_reason','max_wait')}
    health=None
    if (runtime/'health/latest.json').exists():
        snapshot=json.loads((runtime/'health/latest.json').read_text())
        health={'status':snapshot.get('status'),'checked_at':snapshot.get('checked_at'),
                'checks':[{'check':c.get('check'),'status':c.get('status')}
                          for c in snapshot.get('checks',[]) if isinstance(c,dict)]}
    bundle={'schema_version':SCHEMA_VERSION,'label':label,
            'collected_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
            'source':{'runtime':runtime.name},'selection':{'task_ids':sorted(selected)},
            'build':{'supplied':build_supplied,'observed':build_observed},
            'tasks':public_tasks,'retries':public_retries,'workers':workers,
            'maintenance':maintenance,'health':health,'client':_client(client_out),
            'client_rc':(Path(client_rc).read_text().strip() if client_rc and Path(client_rc).exists()
                         else {'missing':True}),
            'missing':missing,
            'file_digests':{name:_sha256(runtime/name) for name in
                            ('tasks.sqlite3','maintenance.json','health/latest.json')},
            'truncated':False}
    return bundle


def full_bundle(runtime,task_ids):
    """Raw rows for private retention; never write this into a public/committed path."""
    runtime=Path(runtime)
    tasks=_rows(_table(runtime/'tasks.sqlite3','tasks'))
    retries=_rows(_table(runtime/'tasks.sqlite3','retries'))
    selected=_expand(task_ids,retries)
    keep=[row for row in tasks if row.get('task_id') in selected]
    return {'schema_version':SCHEMA_VERSION,'selection':{'task_ids':sorted(selected)},
            'tasks':keep,
            'retries':[row for row in retries if row.get('root_task_id') in selected
                       or row.get('retry_task_id') in selected],
            'workers':{db.stem:_rows(_table(db,'work')) for db in sorted(runtime.glob('worker-*.sqlite3'))}}


def private_target(private_out):
    """Validate a private output directory: outside the repo, not a symlink, mode 0700."""
    private=Path(private_out)
    if private.is_symlink():
        raise SystemExit('refusing a symlinked private-out: %s'%private)
    # Compare RESOLVED paths, so the repository root itself, any child and any symlink that
    # points into the repository are all refused (no override switch exists).
    resolved=Path(os.path.realpath(private))
    repo=Path(os.path.realpath(ROOT))
    if resolved==repo or str(resolved).startswith(str(repo)+os.sep):
        raise SystemExit('refusing to write raw bodies inside the repository: %s'%resolved)
    private.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(private,0o700)
    return private


def write_private(private,payload):
    """Create the private bundle 0600 at open time and refuse to overwrite an existing one."""
    target=private/('full-bundle-%s.json'%time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
    if target.exists():
        raise SystemExit('refusing to overwrite an existing private bundle: %s'%target)
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:
        stream.write(json.dumps(payload,ensure_ascii=False,indent=2))
    os.chmod(target,0o600)
    return target


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',default='.runtime-lan')
    parser.add_argument('--out',required=True)
    parser.add_argument('--label',default='drill')
    parser.add_argument('--task-id',action='append',default=[])
    parser.add_argument('--task-ids')
    parser.add_argument('--client-out');parser.add_argument('--client-rc')
    parser.add_argument('--build-supplied');parser.add_argument('--build-observed')
    parser.add_argument('--build-digest',help=argparse.SUPPRESS)
    parser.add_argument('--include-bodies',action='store_true')
    parser.add_argument('--private-out')
    args=parser.parse_args()
    task_ids=list(args.task_id)
    if args.task_ids:
        task_ids+= [part.strip() for part in args.task_ids.split(',') if part.strip()]
    if not task_ids:
        parser.error('at least one --task-id is required (do not dump the whole ledger)')
    bundle=build(args.runtime,task_ids,args.client_out,args.client_rc,args.label,
                 args.build_supplied or args.build_digest,args.build_observed)
    assert_public(bundle)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    (out/'bundle.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2))
    (out/'README.txt').write_text('Public evidence bundle for %s; tasks %s. Status, codes, timings '
                                  'and linkage only; no question or answer bodies.\n'
                                  %(args.label,','.join(bundle['selection']['task_ids'])))
    if args.include_bodies:
        if not args.private_out:
            parser.error('--include-bodies requires --private-out')
        private=private_target(args.private_out)
        target=write_private(private,full_bundle(args.runtime,task_ids))
        print(json.dumps({'private':str(target),'mode':oct(target.stat().st_mode & 0o777)},ensure_ascii=False))
    tasks=bundle['tasks'] if isinstance(bundle['tasks'],list) else []
    print(json.dumps({'out':str(out),'tasks':len(tasks),'retries':len(bundle['retries']),
                      'missing':bundle['missing']},ensure_ascii=False))


if __name__=='__main__':
    raise SystemExit(main())
