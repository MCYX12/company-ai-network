"""Read-only remote progress view; all data follows the caller's Router permissions."""
from datetime import datetime,timezone
import re

NATIVE_FIELDS={'status','source','scope','observed_at','age_seconds','live_sessions',
               'executing_count','progress','history_read','summary','limits','hint','schema_version'}
WORKER_FIELDS={'source','state','observed_at','executing_count','results_pending','progress','scope'}

def status_data(status):
    """Expose status metadata only, even when an adapter sends additional fields."""
    data=status.get('data') if isinstance(status,dict) and status.get('code')=='OK' else None
    native=data.get('native_work') if isinstance(data,dict) else None
    worker=data.get('worker_activity') if isinstance(data,dict) else None
    result={'native_work':{k:v for k,v in native.items() if k in NATIVE_FIELDS} if isinstance(native,dict)
            else {'status':'unknown' if status else 'not_integrated','progress':None,'summary':'未取得工具实时状态。'},
            'worker_activity':{k:v for k,v in worker.items() if k in WORKER_FIELDS} if isinstance(worker,dict)
            else {'state':'unknown','progress':None,'scope':'platform_managed_queries_only'}}
    if isinstance(native,dict):
        fields={'status','source','observed_at','hint','executing_count','total_count','active_count','due_count'}
        for name in ('background_jobs','scheduled_tasks'):
            item=native.get(name)
            if isinstance(item,dict):result['native_work'][name]={k:v for k,v in item.items() if k in fields}
    if status and status.get('code')!='OK':result['status_query_error']=status.get('code','UNKNOWN')
    return result

def remote_work(bridge,target):
    if not isinstance(target,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',target):raise ValueError('Invalid target')
    directory=bridge.call('list_agents',{},None)
    if directory.get('code')!='OK':return directory
    agent=next((a for a in directory['data']['agents'] if a['agent_id']==target),None)
    if not agent:return {'code':'FORBIDDEN','data':None}
    status=bridge.call('get_status',{},target) if 'get_status' in agent.get('capabilities',[]) else None
    return {'code':'OK','data':{'queried_at':datetime.now(timezone.utc).isoformat(),'target':target,
            'presence':agent['presence'],**status_data(status)}}


def remote_progress(bridge,target,project_id):
    for value in [target,project_id]:
        if not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',value):raise ValueError('Invalid target or project')
    directory=bridge.call('list_agents',{},None)
    if directory.get('code')!='OK':return directory
    agent=next((a for a in directory['data']['agents'] if a['agent_id']==target),None)
    if not agent:return {'code':'FORBIDDEN','data':None}
    # Query the actual remote agent, never read its files over SSH.
    record=bridge.call('get_project_progress',{'project_id':project_id},target)
    status=bridge.call('get_status',{},target) if 'get_status' in agent.get('capabilities',[]) else None
    tasks=bridge.call('list_tasks',{'limit':50},None)
    recent=[t for t in tasks.get('data',{}).get('tasks',[]) if t['target']==target] if tasks.get('code')=='OK' else []
    data={'queried_at':datetime.now(timezone.utc).isoformat(),'requester':bridge.identity,'target':target,'device_id':agent['device_id'],'presence':agent['presence'],
          'route':[bridge.identity,'授权 Router',target],'project':record,'recent_own_tasks':recent,'task_scope':'仅本人最近 50 条授权任务中发给该目标的记录，不是目标的全部任务',
          **status_data(status),
          'interpretation':'项目记录来自显式发布；平台任务状态与工具自身会话进度分别呈现；在线不等于工作完成。'}
    if tasks.get('code')!='OK':data['task_query_error']=tasks.get('code','UNKNOWN')
    return {'code':'OK','data':data}
