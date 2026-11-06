import json, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from app import native_work
from tests.helpers.client import load_client_worker

ROOT=Path(__file__).resolve().parents[1]
def snapshot(**extra):
    return dict(schema_version=1,status='idle',observed_at=time.time(),source=native_work.SOURCE,
                scope='desktop_visible_sessions',live_sessions=1,executing_count=0,progress=None,history_read=False,**extra)

class Tests(unittest.TestCase):
    def test_strict_metadata_does_not_accept_body_stale_or_false_idle(self):
        valid=snapshot();self.assertEqual(native_work.validate(valid)['status'],'idle')
        for extra in ({'text':'PRIVATE'}, {'observed_at':time.time()-60}, {'observed_at':float('nan')},
                      {'live_sessions':True},{'executing_count':2},{'status':'running'},
                      {'history_read':True},{'schema_version':True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):native_work.validate({**valid,**extra})
        self.assertEqual(native_work.validate({**valid,'status':'running','executing_count':1})['status'],'running')

    def test_disabled_never_launches_desktop_or_reads_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch('app.native_work.subprocess.Popen') as launch:
            self.assertEqual(native_work.desktop_status({},Path(tmp))['status'],'not_integrated');launch.assert_not_called()

    def test_live_query_is_fixed_and_model_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=root/'app-dir';app.mkdir();home=root/'home';home.mkdir()
            exe=app/'DeepSeek Harness.exe';exe.write_bytes(b'exe');(root/'dsh_desktop_status.cjs').write_text('helper')
            config={'native_work':{'enabled':True,'app':str(app),'home':str(home)}}
            proc=MagicMock();proc.__enter__.return_value=proc;proc.returncode=0
            proc.communicate.return_value=(json.dumps(snapshot()).encode(),None)
            with patch.object(native_work.sys,'platform','win32'),patch.object(native_work.subprocess,'Popen',return_value=proc) as launch:
                result=native_work.desktop_status(config,root)
            self.assertEqual(result['status'],'idle');args,kwargs=launch.call_args
            self.assertEqual(args[0],[str(exe),'--expose-internals',str(root/'dsh_desktop_status.cjs'),str(home)])
            self.assertNotIn('PATH',kwargs['env']);self.assertNotIn('shell',kwargs);self.assertEqual(proc.communicate.call_args.kwargs['timeout'],7)

    def test_extended_work_metadata_is_strict_and_partial_failure_stays_unknown(self):
        now=time.time();source=native_work.SOURCE
        value={**snapshot(),'schema_version':2,
               'background_jobs':{'status':'running','source':source,'observed_at':now,'executing_count':1,'total_count':2},
               'scheduled_tasks':{'status':'scheduled','source':source,'observed_at':now,'active_count':1,'due_count':0,'total_count':2}}
        self.assertEqual(native_work.validate(value)['background_jobs']['executing_count'],1)
        for field in ('background_jobs','scheduled_tasks'):
            for extra in ({'prompt':'PRIVATE'},{'observed_at':now-90},{'total_count':False},{'total_count':0}):
                with self.subTest(field=field,extra=extra),self.assertRaises(ValueError):
                    native_work.validate({**value,field:{**value[field],**extra}})
            partial={**value,field:native_work.unavailable('DESKTOP_STATUS_TIMEOUT')}
            partial[field].pop('progress')
            self.assertEqual(native_work.validate(partial)[field]['status'],'unknown')

    def test_unavailable_and_invalid_output_never_reuse_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);app=root/'app-dir';app.mkdir();home=root/'home';home.mkdir();(app/'DeepSeek Harness.exe').write_bytes(b'exe');(root/'dsh_desktop_status.cjs').write_text('helper')
            cfg={'native_work':{'enabled':True,'app':str(app),'home':str(home)}}
            proc=MagicMock();proc.__enter__.return_value=proc;proc.returncode=1;proc.communicate.return_value=(b'PRIVATE ERROR',None)
            with patch.object(native_work.sys,'platform','win32'),patch.object(native_work.subprocess,'Popen',return_value=proc):
                result=native_work.desktop_status(cfg,root)
                self.assertEqual(result['status'],'unknown');self.assertNotIn('PRIVATE',json.dumps(result))
                for hint in native_work.ERROR_HINTS:
                    proc.communicate.return_value=(json.dumps(native_work.unavailable(hint)).encode(),None)
                    self.assertEqual(native_work.desktop_status(cfg,root)['hint'],hint)
                proc.communicate.return_value=(json.dumps(native_work.unavailable('PRIVATE ERROR')).encode(),None)
                self.assertEqual(native_work.desktop_status(cfg,root)['hint'],'DESKTOP_STATUS_UNAVAILABLE')
                proc.communicate.return_value=(b'PRIVATE ERROR',None)
                proc.returncode=0;self.assertEqual(native_work.desktop_status(cfg,root)['status'],'unknown')
                proc.communicate.side_effect=[subprocess.TimeoutExpired('PRIVATE ARGV',5),(b'',None)]
                with patch.object(native_work,'terminate_process_tree') as stop:
                    self.assertEqual(native_work.desktop_status(cfg,root)['hint'],'DESKTOP_STATUS_TIMEOUT');stop.assert_called_once_with(proc)

    def test_router_rejection_precedes_native_read(self):
        worker=load_client_worker();body={'target_agent_id':'other','action':'get_status','expires_at':time.time()+60}
        with patch.object(native_work,'desktop_status') as read:
            self.assertEqual(worker.query_result({'capabilities':['get_status']},'peer',body,ROOT)[0],'FORBIDDEN');read.assert_not_called()
            body['target_agent_id']='peer';body['expires_at']=time.time()-1
            self.assertEqual(worker.query_result({'capabilities':['get_status']},'peer',body,ROOT)[0],'EXPIRED');read.assert_not_called()

    def test_bundled_node_metadata_and_rpc_contract(self):
        script=r'''
const assert=require('node:assert'),http=require('node:http'),{EventEmitter}=require('node:events');
const h=require(process.argv[1]);
const now=Date.now();
const jobs=h.jobsSummary([{type:'rows',jobs:[{id:'PRIVATE JOB',status:'running',label:'PRIVATE LABEL',detail:'PRIVATE BODY'}]},
 {type:'rows',jobs:[{id:'PRIVATE JOB',status:'running'}]}],now);
assert.equal(jobs.executing_count,1);assert.equal(jobs.total_count,1);assert(!JSON.stringify(jobs).includes('PRIVATE'));
assert.throws(()=>h.jobsSummary([{type:'rows',jobs:[{id:'x',status:'bad'}]}]));
const schedules=h.schedulesSummary([{id:'PRIVATE PLAN',status:'active',scheduledAt:new Date(now-1000).toISOString(),prompt:'PRIVATE PROMPT'}],now);
assert.equal(schedules.active_count,1);assert.equal(schedules.due_count,1);assert(!JSON.stringify(schedules).includes('PRIVATE'));
assert.throws(()=>h.schedulesSummary([{id:'x',status:'active',scheduledAt:'bad'}]));
assert.throws(()=>h.rpc('test','job/kill',{}));
class FakeSocket extends EventEmitter {
 constructor(url,opts){super();assert.equal(url,'ws://127.0.0.1:19387/api/remote.mux');assert.equal(opts.followRedirects,false);queueMicrotask(()=>this.emit('open'));}
 send(data){const m=JSON.parse(data);if(m.type==='open'){assert.equal(m.endpoint,'job/list');assert.deepEqual(Object.keys(m.payload.args),['request']);
  queueMicrotask(()=>this.emit('message',Buffer.from(JSON.stringify({type:'item',streamId:m.streamId,value:{type:'rows',jobs:[{id:'hidden',status:'running',label:'SECRET'}]}})),false));}else assert.equal(m.type,'cancel');}
 terminate(){this.emit('close');}
}
h.jobs('test',[{sessionId:'a'},{sessionId:'b'}],FakeSocket).then(o=>{assert.equal(o.executing_count,1);assert(!JSON.stringify(o).includes('SECRET'));}).catch(e=>{throw e;});
const cookie=h.cookie({kind:'grant',payload:{version:1,secret:Buffer.alloc(32,7).toString('base64url')}},5000);
const claim=JSON.parse(Buffer.from(cookie.split('.')[1],'base64url').toString());assert.equal(claim.issuedAt,4000);assert.equal(claim.expiresAt-claim.issuedAt,20000);assert.equal(claim.authority,'127.0.0.1:19387');
const privateRow={sessionId:'PRIVATE ID',agentAvailable:true,running:true,title:'PRIVATE TITLE',cwd:'PRIVATE PATH',projections:{plan:'PRIVATE PLAN'}};
const r=h.summarize({items:[privateRow,{sessionId:'cold',agentAvailable:false,running:false}]});
assert.equal(r.status,'running');assert.equal(r.executing_count,1);assert.equal(r.live_sessions,1);assert(!JSON.stringify(r).includes('PRIVATE'));
assert.equal(h.summarize({items:[]}).status,'idle');
for(const items of [[{...privateRow,running:'false'}],[privateRow,privateRow],[{...privateRow,agentAvailable:false}]])assert.throws(()=>h.summarize({items}));
assert.throws(()=>h.cookie({kind:'grant',payload:{version:1,secret:'INVALID'}}));
let wire;http.request=(options,callback)=>{assert.equal(options.hostname,'127.0.0.1');assert.equal(options.port,19387);assert.equal(options.path,'/api/session/list');assert.equal(options.method,'POST');
 const req=new EventEmitter();req.setTimeout=()=>{};req.end=body=>{wire=JSON.parse(body);assert.deepEqual(wire.payload,{args:{_request:{}}});
 const res=new EventEmitter();res.statusCode=200;callback(res);res.emit('data',Buffer.from(JSON.stringify({type:'server-response',rpcId:wire.rpcId,result:{ok:true,value:{items:[privateRow]}}})));res.emit('end');};return req;};
h.list('test-cookie').then(v=>{assert.equal(v.status,'running');assert(!JSON.stringify(v).includes('PRIVATE'));console.log('PASS');}).catch(e=>{throw e;});
'''
        r=subprocess.run(['node','-e',script,str(ROOT/'packaging/client/dsh_desktop_status.cjs')],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr);self.assertEqual(r.stdout.strip(),'PASS')

if __name__=='__main__':unittest.main()
