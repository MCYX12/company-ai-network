"""SQLite-consistent runtime snapshots; restore only to a new, disconnected directory."""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time

NAME=re.compile(r'(tasks\.sqlite3|worker-[a-z][a-z0-9-]{0,63}\.sqlite3|accounts\.sqlite3|conversation-access\.sqlite3|directory-requests\.sqlite3|file-access\.sqlite3|roster\.sqlite3|history-access\.sqlite3|replay\.sqlite3|model-budget\.sqlite3|leases\.sqlite3|registry\.json|site\.json|project-retirements\.json|project-records/[a-z][a-z0-9-]{0,63}\.json|project-records/[a-z][a-z0-9-]{0,63}\.meta\.json)\Z')
SCHEMAS={'tasks.sqlite3':{'tasks','task_audit','task_archive','retries'},
         'accounts.sqlite3':{'admins','sessions','invites','members','account_events','login_attempts'},
         'conversation-access.sqlite3':{'approvals','access_events'},
         'directory-requests.sqlite3':{'requests','events'},
         'file-access.sqlite3':{'scopes','file_audit'},
         'roster.sqlite3':{'employment'},
         'history-access.sqlite3':{'requests','events'},
         'replay.sqlite3':{'seen'}, 'model-budget.sqlite3':{'usage','denials'},
         'leases.sqlite3':{'leases','lease_epochs'}}

CRED_NAME=re.compile(r'(certs/[A-Za-z0-9._-]+\.(?:crt|key)|certs/ENTERPRISE_CA\.json|nats\.conf|endpoint\.txt)\Z')
CA_NAME=re.compile(r'enterprise-ca/[A-Za-z0-9._/-]+\Z')

def manifest_name_ok(name):
    return bool(NAME.fullmatch(name) or (name.startswith('authority/') and NAME.fullmatch(name[10:]))
                or CRED_NAME.fullmatch(name) or CA_NAME.fullmatch(name)
                or re.fullmatch(r'jetstream/(manifest\.json|[A-Za-z0-9_-]{1,128}\.snap)', name))

def allowed_tables(name):
    base=Path(name).name
    if base.startswith('worker-') and base.endswith('.sqlite3'):return {'work','worker_state'}
    return SCHEMAS.get(base)

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def readonly(path):
    return sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)

def check_db(path,name):
    allowed=allowed_tables(name)
    if allowed is None:raise ValueError('Unknown database name')
    with closing(readonly(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('SQLite integrity check failed')
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not names or not names<=allowed:raise ValueError('Unexpected database schema')
        return {name:db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in sorted(names)}

def backup(runtime,output,include_credentials=False,include_ca=False,authority_runtime=None,jetstream_snapshot=None):
    runtime=Path(runtime).resolve();output=Path(output).absolute()
    if output.exists():raise ValueError('Backup destination already exists')
    paths=set()
    for pattern in ['tasks.sqlite3','worker-*.sqlite3','accounts.sqlite3','conversation-access.sqlite3','directory-requests.sqlite3','file-access.sqlite3','roster.sqlite3','history-access.sqlite3','replay.sqlite3','model-budget.sqlite3','leases.sqlite3','registry.json','site.json','project-retirements.json','project-records/*.json']:
        paths.update(runtime.glob(pattern))
    if include_credentials:
        paths.update(runtime.glob('certs/*.crt'));paths.update(runtime.glob('certs/*.key'));paths.update(runtime.glob('certs/ENTERPRISE_CA.json'))
        paths.update([runtime/'nats.conf',runtime/'endpoint.txt'])
        paths={p for p in paths if p.exists()}
    if include_ca:
        ca=runtime/'enterprise-ca'
        if ca.is_dir():paths.update(p for p in ca.rglob('*') if p.is_file() and not p.is_symlink())
    from app.common import authority_base
    authority = Path(authority_runtime or authority_base(runtime)).resolve()
    if not authority.is_dir():raise ValueError('Authorization authority directory unavailable')
    sources = [(p, p.relative_to(runtime).as_posix()) for p in paths]
    if authority != runtime:
        for pattern in ['*.sqlite3', 'registry.json', 'site.json', 'project-retirements.json', 'project-records/*.json']:
            for path in sorted(authority.glob(pattern)):
                rel = path.relative_to(authority).as_posix()
                if NAME.fullmatch(rel):sources.append((path, 'authority/' + rel))
    js_manifest = None
    if jetstream_snapshot:
        from scripts import jetstream_backup
        js_manifest = jetstream_backup.verify(jetstream_snapshot)
        for rel in ['manifest.json'] + [entry['file'] for entry in js_manifest['streams']]:
            sources.append((Path(jetstream_snapshot) / rel, 'jetstream/' + rel))
    durable = os.environ.get('POC_HA') == '1' or os.environ.get('POC_JETSTREAM') == '1' or (runtime/'jetstream').is_dir()
    if durable:
        from scripts.jetstream_backup import DEFAULT_STREAMS
        found = {entry['name'] for entry in (js_manifest or {}).get('streams', [])}
        required = DEFAULT_STREAMS if os.environ.get('POC_HA') == '1' else DEFAULT_STREAMS[:2]
        if not set(required) <= found:raise ValueError('HA backup requires all task, DLQ, KV task and lease stream snapshots' if os.environ.get('POC_HA') == '1' else 'Durable backup requires task/DLQ snapshots; use --capture-jetstream')
    excluded = [str(path) for base in {runtime, authority} for path in base.glob('*.sqlite3')
                if not NAME.fullmatch(path.name)]
    if not any(p.suffix=='.sqlite3' for p, _ in sources):raise ValueError('No task or worker database found')
    output.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.snapshot-',dir=output.parent))
    started=time.time();entries=[]
    try:
        for source, rel in sorted(sources, key=lambda item: item[1]):
            allowed=(NAME.fullmatch(rel) or (rel.startswith('authority/') and NAME.fullmatch(rel[10:]))
                     or (jetstream_snapshot and manifest_name_ok(rel) and rel.startswith('jetstream/')) or (include_credentials and CRED_NAME.fullmatch(rel)) or (include_ca and CA_NAME.fullmatch(rel)))
            if not allowed or source.is_symlink() or source.parent.is_symlink():raise ValueError('Unexpected source path: '+rel)
            dest=stage/rel;dest.parent.mkdir(parents=True,exist_ok=True)
            entry={'name':rel}
            if source.suffix=='.sqlite3':
                with closing(readonly(source)) as src,closing(sqlite3.connect(dest)) as dst:
                    def progress(*_):
                        if time.time()-started>60:raise TimeoutError('Backup exceeded time budget')
                    src.backup(dst,pages=128,progress=progress,sleep=.05)
                    dst.execute('PRAGMA journal_mode=DELETE')
                entry['tables']=check_db(dest,rel)
            elif rel.endswith('.json'):
                raw=source.read_bytes();json.loads(raw);dest.write_bytes(raw)
            else:
                dest.write_bytes(source.read_bytes())
            dest.chmod(0o600)
            entry.update(sha256=sha(dest),bytes=dest.stat().st_size);entries.append(entry)
        manifest={'format':1,'created_at':datetime.now(timezone.utc).isoformat(),'consistency':'each SQLite database independently; not a distributed point-in-time snapshot','contains_private_keys':bool(include_credentials or include_ca),'files':entries,
                  'authority_layout':'authority' if authority != runtime else '.',
                  'jetstream_included':bool(js_manifest), 'excluded_databases':excluded,
                  'coverage_complete':not excluded and (not durable or bool(js_manifest))}
        (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');(stage/'manifest.json').chmod(0o600)
        if output.exists():raise ValueError('Backup destination already exists')
        stage.rename(output)
        return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)

def verify(snapshot):
    snapshot=Path(snapshot)
    if snapshot.is_symlink():raise ValueError('Snapshot cannot be a symlink')
    manifest=json.loads((snapshot/'manifest.json').read_text())
    if manifest.get('format')!=1 or not isinstance(manifest.get('files'),list):raise ValueError('Invalid manifest')
    if manifest.get('authority_layout', '.') not in ('.', 'authority'):raise ValueError('Invalid authority layout')
    seen=set()
    for entry in manifest['files']:
        name=entry['name']
        if not isinstance(name,str) or not manifest_name_ok(name) or name in seen:raise ValueError('Invalid manifest path')
        seen.add(name);path=snapshot/name
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():raise ValueError('Invalid snapshot file')
        if sha(path)!=entry['sha256'] or path.stat().st_size!=entry['bytes']:raise ValueError('Snapshot checksum mismatch')
        if path.suffix=='.sqlite3':
            if check_db(path,name)!=entry['tables']:raise ValueError('Snapshot table mismatch')
        elif name.endswith('.json'):json.loads(path.read_bytes())
    if not any(n.endswith('.sqlite3') for n in seen):raise ValueError('No database in snapshot')
    if any(name.startswith('jetstream/') for name in seen):
        from scripts import jetstream_backup
        jetstream_backup.verify(snapshot / 'jetstream')
    return manifest

def restore(snapshot,destination):
    snapshot=Path(snapshot);destination=Path(destination).absolute()
    if destination.exists():raise ValueError('Restore destination must not exist; never overwrite a live runtime')
    manifest=verify(snapshot)
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=Path(tempfile.mkdtemp(prefix='.restore-',dir=destination.parent));held=0;interrupted=0
    try:
        for entry in manifest['files']:
            target=stage/entry['name'];target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(snapshot/entry['name'],target);target.chmod(0o600)
            if sha(target)!=entry['sha256']:raise ValueError('Source changed during restore')
        bases = [stage] + ([stage / 'authority'] if (stage / 'authority').exists() else [])
        sessions = 0
        for base in bases:
            leases=base/'leases.sqlite3'
            if leases.exists():
                with closing(sqlite3.connect(leases)) as db,db:
                    db.execute("UPDATE leases SET expires=0,holder='restore-review',token='' ")
            tasks=base/'tasks.sqlite3'
            if tasks.exists():
                with closing(sqlite3.connect(tasks)) as db,db:
                    rows=db.execute("SELECT task_id FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchall()
                    for (task_id,) in rows:
                        db.execute("UPDATE tasks SET state='failed',result=? WHERE task_id=?",(json.dumps({'code':'RESTORE_REVIEW_REQUIRED','data':None}),task_id))
                        db.execute('INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,?,?)',(task_id,'restore_review_required',time.time()))
                    held+=len(rows)
            for path in base.glob('worker-*.sqlite3'):
                with closing(sqlite3.connect(path)) as db,db:
                    interrupted+=db.execute("UPDATE work SET state='done',result=?,acked=0 WHERE state='running'",(json.dumps({'code':'WORKER_INTERRUPTED','data':None}),)).rowcount
            accounts=base/'accounts.sqlite3'
            if accounts.exists():
                with closing(sqlite3.connect(accounts)) as db,db:
                    sessions+=db.execute('DELETE FROM sessions').rowcount
                    db.execute('DELETE FROM login_attempts')
        def count_rows(name,table):
            authoritative = stage / manifest.get('authority_layout', '.')
            path=authoritative/name
            if not path.exists():path=stage/name
            if not path.exists():return 0
            with closing(sqlite3.connect(path)) as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]
        credentials_restored=manifest.get('contains_private_keys',False)
        report={'format':1,'restored_at':datetime.now(timezone.utc).isoformat(),'snapshot_created_at':manifest['created_at'],'pending_tasks_held':held,'interrupted_workers':interrupted,'sessions_invalidated':sessions,'ledgers':{'members':count_rows('accounts.sqlite3','members'),'conversation_approvals':count_rows('conversation-access.sqlite3','approvals'),'directory_requests':count_rows('directory-requests.sqlite3','requests'),'file_scopes':count_rows('file-access.sqlite3','scopes'),'employment':count_rows('roster.sqlite3','employment'),'history_requests':count_rows('history-access.sqlite3','requests'),'replay_seen':count_rows('replay.sqlite3','seen')},'authority_layout':manifest.get('authority_layout', '.'), 'jetstream_included':manifest.get('jetstream_included', False),
                'jetstream_restore_required':manifest.get('jetstream_included', False),
                'coverage_complete':manifest.get('coverage_complete',False), 'excluded_databases':manifest.get('excluded_databases',[]),
                'automatic_replay':False,'network_credentials_restored':bool(credentials_restored),'note':'Reconcile current authorization and post-snapshot execution before reconnecting. Restored admin sessions are invalidated; not a full infrastructure recovery.'}
        (stage/'restore-report.json').write_text(json.dumps(report,indent=2)+'\n');(stage/'restore-report.json').chmod(0o600)
        if destination.exists():raise ValueError('Restore destination already exists')
        stage.rename(destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)

def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser();subs=parser.add_subparsers(dest='command',required=True)
    b=subs.add_parser('backup');b.add_argument('--runtime',type=Path,required=True);b.add_argument('--output',type=Path,required=True);b.add_argument('--include-credentials',action='store_true',help='also back up certs, keys, nats.conf and endpoint');b.add_argument('--include-ca',action='store_true',help='also back up the enterprise CA directory (contains the issuing key)')

    v=subs.add_parser('verify');v.add_argument('--snapshot',type=Path,required=True)
    r=subs.add_parser('restore');r.add_argument('--snapshot',type=Path,required=True);r.add_argument('--destination',type=Path,required=True)
    b.add_argument('--authority-runtime',type=Path,help='shared authority (otherwise POC_AUTHZ_RUNTIME)')
    b.add_argument('--jetstream-snapshot',type=Path,help='verified native JetStream snapshot directory')
    b.add_argument('--capture-jetstream',action='store_true',help='capture every stream in the connected account before backing up')
    b.add_argument('--identity',default='router')
    args=parser.parse_args()
    if args.command=='backup' and args.capture_jetstream:
        if args.jetstream_snapshot:raise ValueError('Choose capture or an existing snapshot')
        os.environ['POC_RUNTIME']=str(args.runtime.resolve())
        if not os.environ.get('NATS_URL') and (args.runtime/'endpoint.txt').exists():os.environ['NATS_URL']=(args.runtime/'endpoint.txt').read_text().strip()
        import asyncio
        from scripts import jetstream_backup
        async def capture(directory):
            from app.common import connect
            nc=await connect(args.identity,timeout=10)
            try:
                streams=[info.config.name for info in await nc.jetstream().streams_info()]
                await jetstream_backup.snapshot(nc,directory,streams)
            finally:await nc.close()
        with tempfile.TemporaryDirectory(prefix='company-js-backup-') as folder:
            directory=Path(folder)/'jetstream'
            asyncio.run(capture(directory))
            result=backup(args.runtime,args.output,args.include_credentials,args.include_ca,args.authority_runtime,directory)
    else:
        result=backup(args.runtime,args.output,args.include_credentials,args.include_ca,args.authority_runtime,args.jetstream_snapshot) if args.command=='backup' else verify(args.snapshot) if args.command=='verify' else restore(args.snapshot,args.destination)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('coverage_complete',False) else 2
if __name__=='__main__':raise SystemExit(main())
