"""Owner-selected JSONL exports; local closed-vocabulary indexing, never cloud transcripts.

This adapter accepts explicit company_harness_history_v1 exports, not arbitrary native
Harness storage. Existing desktop conversations are never discovered automatically.
"""
import hashlib,json,math,time
from pathlib import Path
from .client_material import read_bytes,independent,safe_name
from .history_access import decision
from .history_progress import signals,merge
SOURCE='local_harness_history_index'
MAX_BYTES=1024*1024

def policy(runtime,caller=None,target=None,now=None):
    now=time.time() if now is None else now
    try:raw,_=read_bytes(runtime,'harness-history-access.json',8192)
    except FileNotFoundError:return None
    p=json.loads(raw)
    if not isinstance(p,dict) or set(p)!={'enabled','target','callers','expires','root','sessions'}:raise ValueError('History policy schema')
    if p['enabled'] is not True:return None
    if (not isinstance(p['expires'],(float,int)) or isinstance(p['expires'],bool) or not math.isfinite(p['expires'])
        or p['expires']<=now or not isinstance(p['callers'],list) or not p['callers']
        or len(p['callers'])>20 or not all(isinstance(x,str) and 1<=len(x)<=64 for x in p['callers'])
        or not isinstance(p['target'],str) or not isinstance(p['root'],str) or not Path(p['root']).is_absolute()
        or not isinstance(p['sessions'],list) or not 1<=len(p['sessions'])<=20):raise ValueError('History consent invalid or expired')
    if caller is not None and (caller not in p['callers'] or target!=p['target']):raise PermissionError('History owner consent denied')
    for session in p['sessions']:
        if not isinstance(session,dict) or set(session)!={'file','project_id'}:raise ValueError('Session selection schema')
        safe_name(session['file'])
        if not session['file'].endswith('.jsonl') or not isinstance(session['project_id'],str) or not 1<=len(session['project_id'])<=64:raise ValueError('Invalid export selection')
    p['version']=hashlib.sha256(raw).hexdigest()
    return p

def consent_status(runtime):
    try:p=policy(runtime)
    except (OSError,ValueError,TypeError,KeyError,PermissionError):p=None
    return {'enabled':bool(p),'version':p['version'] if p else None,'expires':p['expires'] if p else 0}

def answer(runtime,envelope,grants):
    payload=envelope.get('payload') or {};caller=payload.get('caller');target=envelope.get('target');projects=payload.get('projects')
    try:
        if not isinstance(caller,str) or not caller or not isinstance(target,str) or not target:
            return {'code':'FORBIDDEN','data':None}
        p=policy(runtime,caller,target)
        if not p:return {'code':'HISTORY_NOT_CONFIGURED','data':{'hint':'Owner-selected history exports are disabled'}}
        if not isinstance(projects,list) or not projects:return {'code':'HISTORY_SCOPE_REQUIRED','data':None}
        selected=[s for s in p['sessions'] if s['project_id'] in projects]
        if not selected or any(decision(caller,target,grants,scope=s['project_id'])=='DENIED' for s in selected):return {'code':'FORBIDDEN','data':None}
        def check():
            current=policy(runtime,caller,target)
            if not current or current['version']!=p['version'] or time.time()>=envelope['deadline']:raise PermissionError('History consent changed')
        totals=0;rows=[];skipped=0
        for session in selected:
            check();raw,info=read_bytes(p['root'],session['file'],MAX_BYTES)
            totals+=len(raw)
            if totals>2*MAX_BYTES:raise ValueError('History total limit')
            if not raw.endswith(b'\n'):raise ValueError('Torn history export')
            lines=raw.splitlines()
            if len(lines)>4000:raise ValueError('History event limit')
            header=json.loads(lines[0])
            if header!={'schema':'company_harness_history_v1','project_id':session['project_id']}:raise ValueError('History export header')
            found=[];count=0
            for line in lines[1:]:
                entry=json.loads(line)
                if (not isinstance(entry,dict) or set(entry)!={'role','text','timestamp'} or entry['role'] not in {'user','assistant'}
                    or not isinstance(entry['text'],str) or len(entry['text'])>20000
                    or not isinstance(entry['timestamp'],(int,float)) or isinstance(entry['timestamp'],bool)
                    or not math.isfinite(entry['timestamp']) or not 0<=entry['timestamp']<=time.time()+300):raise ValueError('Invalid history event')
                count+=1;merge(found,signals(entry['text'],entry['role']),entry['timestamp'])
            rows.append({'project_id':session['project_id'],'messages':count,'signals':found,'export_sha256':hashlib.sha256(raw).hexdigest()})
            check()
        return {'code':'OK','data':{'synthetic':False,'source':SOURCE,'text':'本机获准导出会话的历史信号；历史自述不代表当前完成，未向模型发送聊天原文。',
            'projects':rows,'history_projects':sorted({s['project_id'] for s in selected}),
            'owner_consent':{'version':p['version'],'expires':p['expires']},'coverage':{'files':len(rows),'read_bytes':totals,'limited':True,'native_store_read':False},'progress':None}}
    except PermissionError:return {'code':'FORBIDDEN','data':None}
    except (OSError,ValueError,TypeError,KeyError):return {'code':'HISTORY_UNAVAILABLE','data':{'hint':'Owner policy or selected export unavailable; no cloud fallback'}}

def native_status(runtime):
    """Explicit owner-published observation. Never infer activity from log/file timestamps."""
    unknown={'status':'not_integrated','summary':'尚未配置工具状态发布，无法判断桌面会话当前工作进度。'}
    try:raw,_=read_bytes(runtime,'native-work.json',4096)
    except FileNotFoundError:return unknown
    except (OSError,ValueError):return {'status':'unknown','summary':'工具状态文件不可安全读取。','progress':None}
    try:
        p=json.loads(raw)
        if (not isinstance(p,dict) or set(p)!={'schema_version','status','observed_at'} or p['schema_version']!=1
            or isinstance(p['schema_version'],bool) or p['status'] not in {'idle','running','blocked','unknown'} or isinstance(p['observed_at'],bool)
            or not isinstance(p['observed_at'],(int,float)) or not math.isfinite(p['observed_at'])
            or p['observed_at']>time.time()+30):raise ValueError('Native status invalid')
        age=max(0,time.time()-p['observed_at'])
        return {**p,'source':'owner_published_tool_status','status':p['status'] if age<=120 else 'stale','age_seconds':age,'progress':None}
    except (ValueError,TypeError):return {'status':'unknown','summary':'工具状态发布无效，无法判断。','progress':None}
