"""Synthetic fixture plus real, fixed-command, read-only CLI status adapters."""
import json
import os
import time
from .registry import capabilities
from .inventory import COMMANDS, inspect_tool
from .project_records import read_progress

def describe(row):
    state='ready' if row['tool']=='fixture' else 'status_only' if capabilities(row) else 'not_integrated'
    return {'tool':row['tool'],'adapter_status':state,'capabilities':capabilities(row)}

def execute(row, action, payload):
    if action not in capabilities(row):
        return 'UNSUPPORTED_ACTION',None
    if row['tool']!='fixture' and action=='get_project_progress':
        return read_progress(row,payload)
    if row['tool']!='fixture':
        if row['tool'] not in COMMANDS:
            # Declared capabilities on a self-developed agent are served by that
            # agent's own worker; the built-in worker has no adapter for it.
            return 'TOOL_UNAVAILABLE',{'agent_id':row['agent_id'],'tool':row['tool'],'hint':'SELF_DEVELOPED_AGENT_RUNS_ITS_OWN_WORKER'}
        status=inspect_tool(row['tool'])
        data={'synthetic':False,'agent_id':row['agent_id'],'source':'local_cli_version',**status}
        return ('OK' if status['adapter_status']=='status_only' else 'TOOL_UNAVAILABLE'),data
    # Opt-in load-test knobs; both default to 0 so normal behaviour is unchanged.
    delay_ms=int(os.environ.get('POC_FIXTURE_DELAY_MS','0') or 0)
    if delay_ms>0:time.sleep(delay_ms/1000)
    data={'synthetic':True,'source_id':'fixture/demo-project','agent_id':row['agent_id']}
    if action=='get_status':
        data['status']='available'
    else:
        data.update(progress=0.65,summary='合成测试数据：正在进行接口联调')
    target_bytes=int(os.environ.get('POC_FIXTURE_RESPONSE_BYTES','0') or 0)
    if target_bytes>0:
        base=len(json.dumps(data,ensure_ascii=False).encode())
        data['padding']='x'*max(0,target_bytes-base)
    return 'OK',data
