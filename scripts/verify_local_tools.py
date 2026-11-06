"""Discover real installed CLIs, enroll isolated identities and verify through NATS."""
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bootstrap

async def verify(runtime,evidence):
    from app.inventory import discover
    from app.common import connect,request,query
    from scripts.enroll_agent import enroll
    from scripts.run_local import Stack
    discovery=await asyncio.to_thread(discover)
    (evidence/'discovery.json').write_text(json.dumps(discovery,ensure_ascii=False,indent=2))
    matched=[t for t in discovery['tools'] if t['adapter_status']=='status_only']
    if not matched:
        raise RuntimeError('No supported CLI detected; this test cannot be reported as passed')
    identities=[]
    for tool in matched:
        identity='ubuntu-'+tool['tool']
        enroll(runtime,dict(agent_id=identity,device_id='ubuntu-local',employee_id='demo-a',
                            tool=tool['tool'],projects=['demo-project'],enabled=True))
        identities.append(identity)
    profile=runtime/'device.json'
    profile.write_text(json.dumps({'device_id':'ubuntu-local','agents':identities}))
    stack=Stack(evidence)
    client=None
    results=[]
    def record(test,data=None):
        results.append({'test':test,'status':'PASS','data':data})
        print('PASS '+test,flush=True)
    try:
        stack.boot()
        stack.stop_child('ag-b')
        # Exclude the synthetic responder from capability routing in this real-tool test.
        registry=json.loads((runtime/'registry.json').read_text())
        for row in registry['agents']:
            if row['agent_id']=='ag-b':row['enabled']=False
        candidate=runtime/'registry.new.json'
        candidate.write_text(json.dumps(registry));candidate.replace(runtime/'registry.json')
        stack.start('local-tools',[sys.executable,'-u','-m','app.device','--config',str(profile)])
        client=await connect('ag-a')
        def listing():
            body=request();body.pop('target_agent_id');body['action']='list_agents';body['payload']={}
            return body
        for _ in range(50):
            result=await query(client,'ag-a',listing())
            ready=[r for r in result['data']['agents'] if r['agent_id'] in identities
                   and r['presence']=='online' and 'get_status' in r['available_capabilities']]
            if len(ready)==len(identities):break
            await asyncio.sleep(.1)
        else:raise AssertionError('Real adapter registration not ready')
        record('automatic discovery, matching, certificate enrollment and registration',ready)
        for tool,identity in zip(matched,identities):
            body=request(target=identity,action='get_status',project=None)
            result=await query(client,'ag-a',body)
            data=result.get('data',{})
            assert result['code']=='OK',result
            assert data['synthetic'] is False and data['version']==tool['version']
            assert data['tool']==tool['tool'] and data['agent_id']==identity
            assert data['session_status']=='not_inspected' and data['task_execution_supported'] is False
            record(tool['tool']+' status round trip via TLS/NATS/Router',result)
            result=await query(client,'ag-a',request(target=identity))
            assert result['code']=='FORBIDDEN',result
            record(tool['tool']+' cannot claim synthetic project progress')
        body=request(action='get_status',project=None)
        body.pop('target_agent_id');body['capability']='get_status'
        result=await query(client,'ag-a',body)
        assert result['code']=='OK' and result['data']['agent_id'] in identities
        assert result['data']['synthetic'] is False
        record('capability routing selects a real installed CLI status adapter',result)
    except BaseException as error:
        results.append({'test':'suite','status':'FAIL','error':repr(error)})
        raise
    finally:
        if client:await client.close()
        stack.close()
        (evidence/'results.json').write_text(json.dumps({'scope':'installed CLI read-only status through real NATS',
            'results':results,'not_tested':['model inference','existing conversations','actual task delegation',
            'macOS','Windows','OpenClaw adapter','Hermes adapter']},ensure_ascii=False,indent=2))
        print('Evidence: '+str(evidence),flush=True)

if __name__=='__main__':
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence=ROOT/'evidence'/('real-tools-'+stamp);evidence.mkdir()
    with tempfile.TemporaryDirectory(prefix='real-tools-',dir=ROOT/'.runtime') as temp:
        runtime=Path(temp)
        os.environ['POC_RUNTIME']=str(runtime)
        os.environ['NATS_SERVER']=str(ROOT/'.runtime/bin/nats-server')
        bootstrap.RUNTIME=runtime;bootstrap.main()
        asyncio.run(verify(runtime,evidence))
