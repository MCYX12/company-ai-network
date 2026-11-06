"""Read-only operational checks. Healthy is not enterprise acceptance."""
import argparse
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import time
from .common import RUNTIME,connect,query,request
from . import project_state


def expiry_status(seconds):
    return 'fail' if seconds<=0 else 'warn' if seconds<72*3600 else 'ok'


def local_checks(runtime):
    runtime=Path(runtime);checks=[]
    def add(name,status,detail):checks.append({'check':name,'status':status,'detail':detail})
    certs=list((runtime/'certs').glob('*.crt'))
    if not certs:add('certificates','fail','No certificates found')
    for path in sorted(certs):
        try:
            result=subprocess.run(['openssl','x509','-in',str(path),'-noout','-enddate'],capture_output=True,text=True,timeout=3,check=True)
            expiry=ssl.cert_time_to_seconds(result.stdout.strip().removeprefix('notAfter='));seconds=expiry-time.time()
            add('certificate:'+path.name,expiry_status(seconds),{'expires_at':datetime.fromtimestamp(expiry,timezone.utc).isoformat(),'remaining_hours':round(seconds/3600,1)})
        except Exception:add('certificate:'+path.name,'fail','Cannot parse certificate expiry')
    keys=list((runtime/'certs').glob('*.key'))
    insecure=[p.name for p in keys if p.is_symlink() or p.stat().st_mode & 0o077]
    add('private_key_permissions','fail' if insecure or not keys else 'ok',{'insecure_files':insecure,'key_count':len(keys)})
    databases=list(runtime.glob('tasks.sqlite3'))+list(runtime.glob('worker-*.sqlite3'))
    if not databases:add('databases','fail','No task databases found')
    for path in sorted(databases):
        try:
            if path.is_symlink():raise ValueError('symlink')
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
            try:valid=db.execute('PRAGMA quick_check').fetchall()==[('ok',)]
            finally:db.close()
            add('database:'+path.name,'ok' if valid else 'fail','SQLite quick_check')
        except Exception:add('database:'+path.name,'fail','Unable to check database')
    ledger=runtime/'tasks.sqlite3'
    if ledger.exists():
        try:
            if ledger.is_symlink():raise ValueError('symlink')
            db=sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
            try:
                pending=db.execute("SELECT count(*) FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchone()[0]
                queued=db.execute("SELECT count(*) FROM tasks WHERE state='queued'").fetchone()[0]
                running=db.execute("SELECT count(*) FROM tasks WHERE state='running'").fetchone()[0]
                oldest=db.execute("SELECT min(created_at) FROM tasks WHERE state NOT IN ('succeeded','failed','expired','cancelled')").fetchone()[0]
                # Two counters: the lifetime total only ever grows, so alerting keys off the
                # 24h window (see scripts/alerts.py) while the total stays visible for audit.
                # The 24h window is also grouped by target (top 10) so a single noisy device
                # is distinguishable from a platform-wide delivery problem.
                cutoff=time.time()-86400
                dead=0;dead_recent=0;by_target={}
                for value,created,target in db.execute("SELECT result,created_at,target FROM tasks WHERE state='failed' AND result IS NOT NULL"):
                    if 'DELIVERY_FAILED' in (value or ''):
                        dead+=1
                        if (created or 0)>cutoff:
                            dead_recent+=1
                            name=target or 'unknown'
                            by_target[name]=by_target.get(name,0)+1
            finally:db.close()
            age=time.time()-oldest if oldest else 0
            add('task_ledger','warn' if pending>=400 or age>600 else 'ok',
                {'pending':pending,'queued':queued,'running':running,'oldest_pending_seconds':round(age,1),
                 'dead_letter_delivery_failed':dead,'dead_letter_delivery_failed_24h':dead_recent,
                 'dead_letter_by_target_24h':dict(sorted(by_target.items(),key=lambda item:(-item[1],item[0]))[:10])})
        except Exception:add('task_ledger','fail','Unable to inspect task ledger')
    worker_dbs=sorted(runtime.glob('worker-*.sqlite3'))
    if worker_dbs:
        unacked=0;oldest=None;unreadable=0
        for path in worker_dbs:
            try:
                if path.is_symlink():raise ValueError('symlink')
                db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
                try:
                    unacked+=db.execute("SELECT count(*) FROM work WHERE state='done' AND acked=0").fetchone()[0]
                    columns={row[1] for row in db.execute('PRAGMA table_info(work)')}
                    if 'updated_at' in columns:
                        value=db.execute("SELECT min(updated_at) FROM work WHERE state='done' AND acked=0").fetchone()[0]
                        if value:oldest=value if oldest is None else min(oldest,value)
                finally:db.close()
            except (sqlite3.Error,OSError,ValueError):unreadable+=1
        age=time.time()-oldest if oldest else 0
        add('worker_outbox','fail' if unreadable else 'warn' if unacked>=100 or age>3600 else 'ok',
            {'files':len(worker_dbs),'unacked':unacked,'oldest_unacked_seconds':round(age,1),'unreadable':unreadable})
    # 项目记录新鲜度：在 6 小时窗口**关闭前**提醒刷新（发布由 scripts/refresh_project_record.py 负责，
    # 它只在派生事实变化时才发布，因此没人改内容时这里会照实报警，而不是被静默刷绿）。
    folder=runtime/'project-records'
    records=sorted(p for p in folder.glob('*.json') if not project_state.is_sidecar(p.name)) if folder.is_dir() else []
    if not records:
        add('project_records','warn',{'published':0,'note':'尚未发布任何项目记录；相关提问会得到 DATA_UNAVAILABLE'})
    for path in records:
        try:
            from .project_retirement import retired
            if retired(runtime,path.stem):
                add('project_record:'+path.stem,'fail',{'error':'RETIRED_RECORD_PRESENT'})
                continue
            if path.is_symlink():raise ValueError('symlink')
            raw=path.read_bytes()
            if len(raw)>16384:raise ValueError('oversize')
            record=json.loads(raw)
            updated=datetime.fromisoformat(record['updated_at'])
            if updated.tzinfo is None:raise ValueError('timezone')
            hours=(datetime.now(timezone.utc)-updated).total_seconds()/3600
            fresh_hours=round(project_state.FRESH_SECONDS/3600,2)
            warn_hours=round(fresh_hours*5/6,2)          # 提前 1/6 个窗口提醒，别等它真的 stale
            shot=project_state.snapshot(record,now=time.time())
            status='ok' if hours<=warn_hours else 'warn' if hours<=24 else 'fail'
            # 人工叙述的确认时钟独立于机器事实的重算时钟：事实被自动重算不能替人确认 summary/blockers。
            meta=project_state.meta_record(folder,path.stem)
            narrative_age,narrative_clock=project_state.confirmation_clock(record,meta,now=time.time())
            narrative_hours=None if narrative_age is None else round(narrative_age/3600,2)
            note=shot['note'] if status!='ok' else '资料在新鲜窗口内'
            if narrative_hours is not None and narrative_hours>warn_hours and status=='ok':
                status='warn'
                note='机器事实已重算，但人工叙述自 %s 起未再确认（%s 时钟）' % (meta.get('confirmed_at') or record['updated_at'],narrative_clock)
            if narrative_hours is not None and narrative_hours>24 and status!='fail':
                status='fail'
                note='人工叙述自 %s 起未再确认（%s 时钟）' % (meta.get('confirmed_at') or record['updated_at'],narrative_clock)
            add('project_record:'+path.stem,status,
                {'freshness':shot['freshness'],'age_hours':round(hours,2),'updated_at':record['updated_at'],
                 'fresh_window_hours':fresh_hours,'warn_after_hours':warn_hours,
                 'confirmed_at':meta.get('confirmed_at'),'narrative_clock':narrative_clock,
                 'narrative_age_hours':narrative_hours,
                 'refresh_with':'bash scripts/publish_record.sh "<这次确认或变更的一句话>"',
                 'note':note})
        except Exception:
            add('project_record:'+path.stem,'fail','Unreadable or invalid published record')
    # Harness 资料访问边界可见性：没有 cwd 也没有 sandbox 时，问答子进程会继承平台工作目录。
    try:
        harness_path=runtime/'harness.json'
        harness_tools={}
        if harness_path.is_file() and not harness_path.is_symlink():
            raw=harness_path.read_bytes()
            if len(raw)<=16384:
                harness_tools=(json.loads(raw).get('tools') or {})
        if not isinstance(harness_tools,dict):harness_tools={}
        with_boundary,no_boundary=[],[]
        for name,entry in harness_tools.items():
            entry=entry if isinstance(entry,dict) else {}
            sandbox=entry.get('sandbox');cwd=entry.get('cwd')
            if (isinstance(sandbox,list) and sandbox and all(isinstance(x,str) and x for x in sandbox)) or \
               (isinstance(cwd,str) and cwd and os.path.isdir(cwd)):
                with_boundary.append(name)
            else:
                no_boundary.append(name)
        if no_boundary:
            add('harness_isolation','warn',{'harness_tools':sorted(harness_tools),
                'with_boundary':with_boundary,'no_boundary':sorted(no_boundary),
                'note':'这些 harness 未配置 cwd 或 sandbox，子进程会继承平台工作目录；建议配置 cwd 或 sandbox'})
        else:
            add('harness_isolation','ok',{'harness_tools':sorted(harness_tools),'with_boundary':with_boundary})
    except (OSError,ValueError,TypeError,KeyError):
        add('harness_isolation','warn','无法读取 harness.json')
    # 授权控制面：HA 部署必须声明单一权威，否则两台 Router 可能从不同副本读取授权/撤权状态。
    try:
        from . import control_plane
        problems=control_plane.ha_problems(runtime)
        detail={'shared_declared':os.environ.get('POC_AUTHZ_SHARED')=='1',
                'digest':control_plane.authz_digest(runtime)[:12],
                'compare_with':'python -m scripts.check_authz_consistency --runtime <A> --runtime <B>'}
        if problems:
            add('authz_consistency','warn',{'problems':problems,**detail})
        else:
            add('authz_consistency','ok',detail)
    except (OSError,ValueError,TypeError,KeyError):
        add('authz_consistency','warn','无法计算授权控制面摘要')
    try:
        free=shutil.disk_usage(runtime).free;add('disk_space','warn' if free<2*1024**3 else 'ok',{'free_gib':round(free/1024**3,2),'warning_below_gib':2})
    except OSError:add('disk_space','fail','Cannot inspect runtime storage')
    # OIDC 身份同步状态：未配置不影响健康；失败/过期进入管理员 checks（无外部通知）。
    sync_path=runtime/'health'/'oidc-sync.json'
    if sync_path.exists():
        try:
            sync=json.loads(sync_path.read_text())
            from .oidc_status import valid as valid_status
            if not valid_status(sync):raise ValueError('Invalid OIDC sync snapshot')
            state=sync.get('status');failed=int(sync.get('failed') or 0)
            checked=sync.get('checked_at') or 0
            age_hours=(time.time()-checked)/3600 if checked else None
            base_detail={'status':state,'failed':failed,'applied':sync.get('applied'),
                         'skipped':sync.get('skipped'),'last_error':sync.get('last_error'),
                         'checked_at':checked,'last_success':sync.get('last_success')}
            if failed or state in ('failed','partial','no_input'):
                add('oidc_sync','fail' if (state in ('failed','no_input') or (failed and not sync.get('applied'))) else 'warn',base_detail)
            elif age_hours is not None and age_hours>24:
                add('oidc_sync','warn',{**base_detail,'age_hours':round(age_hours,2)})
            else:
                add('oidc_sync','ok',base_detail)
        except (OSError,ValueError):
            add('oidc_sync','fail','OIDC_SNAPSHOT_INVALID: unreadable or invalid OIDC sync status')
    else:
        add('oidc_sync','ok',{'configured':False,'note':'未配置 OIDC 定时同步'})
    return checks


async def check(identity,runtime=RUNTIME):
    checks=local_checks(runtime);nc=None
    async def quiet(*_):pass
    try:
        nc=await asyncio.wait_for(connect(identity,max_reconnect_attempts=0,error_cb=quiet,disconnected_cb=quiet,reconnected_cb=quiet),8)
        body=request(identity,'ag-b');body['action']='list_agents';body['payload']={};body.pop('target_agent_id',None)
        result=await asyncio.wait_for(query(nc,identity,body),8)
        if result['code']!='OK':raise ValueError('Directory unavailable')
        agents=result['data']['agents'];checks.append({'check':'authorized_directory','status':'ok','detail':{'visible':len(agents),'online':sum(a['presence']=='online' for a in agents)}})
    except Exception:checks.append({'check':'authorized_directory','status':'fail','detail':'TLS connection or authorized Router query failed'})
    finally:
        if nc:await nc.close()
    status='fail' if any(c['status']=='fail' for c in checks) else 'warn' if any(c['status']=='warn' for c in checks) else 'ok'
    return {'checked_at':datetime.now(timezone.utc).isoformat(),'identity':identity,'status':status,'scope':'operational health only; not production readiness or security certification','checks':checks}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--identity',required=True);args=parser.parse_args()
    report=asyncio.run(check(args.identity));print(json.dumps(report,ensure_ascii=False,indent=2));raise SystemExit({'ok':0,'fail':1,'warn':2}[report['status']])
if __name__=='__main__':main()
