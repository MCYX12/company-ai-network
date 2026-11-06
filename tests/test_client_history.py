import json,tempfile,time,unittest,os
from pathlib import Path
from unittest.mock import patch
from app.client_history import answer,policy,consent_status,native_status
from app.project_records import read_progress
class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.rt=self.root/'runtime';self.rt.mkdir();self.exports=self.root/'exports';self.exports.mkdir();self.now=time.time()
  self.p={'enabled':True,'target':'win-1','callers':['caller'],'expires':self.now+60,'root':str(self.exports),'sessions':[{'file':'a.jsonl','project_id':'demo'}]};self.write_policy()
  (self.exports/'a.jsonl').write_text(json.dumps({'schema':'company_harness_history_v1','project_id':'demo'})+'\n'+json.dumps({'role':'assistant','text':'测试已完成。下一步需要部署服务。','timestamp':self.now})+'\n')
  self.env={'target':'win-1','payload':{'caller':'caller','projects':['demo']},'deadline':self.now+60}
 def write_policy(self):(self.rt/'harness-history-access.json').write_text(json.dumps(self.p))
 def tearDown(self):self.tmp.cleanup()
 def test_local_closed_vocabulary_no_transcripts(self):
  r=answer(self.rt,self.env,[]);self.assertEqual(r['code'],'OK');s=json.dumps(r,ensure_ascii=False);self.assertNotIn('下一步需要部署服务',s);self.assertIn('历史自述已完成',s);self.assertFalse(r['data']['coverage']['native_store_read'])
 def test_disabled_expired_other_caller_and_scope(self):
  self.p['enabled']=False;self.write_policy();self.assertEqual(answer(self.rt,self.env,[])['code'],'HISTORY_NOT_CONFIGURED')
  self.p['enabled']=True;self.p['expires']=self.now-1;self.write_policy();self.assertFalse(consent_status(self.rt)['enabled'])
  self.p['expires']=self.now+60;self.write_policy();env={**self.env,'payload':{'caller':'other','projects':['demo']}};self.assertEqual(answer(self.rt,env,[])['code'],'FORBIDDEN')
  env={**self.env,'payload':{'caller':'caller','projects':['private']}};self.assertEqual(answer(self.rt,env,[])['code'],'FORBIDDEN')
 def test_administrator_veto_and_narrowing(self):
  for g in [dict(caller='caller',target='win-1',state='denied'),dict(caller='caller',target='win-1',state='approved',expires=self.now+60,scope='private')]:self.assertEqual(answer(self.rt,self.env,[g])['code'],'FORBIDDEN')
 def test_missing_caller_never_bypasses_owner_selection(self):
  env={**self.env,'payload':{'projects':['demo']}}
  self.assertEqual(answer(self.rt,env,[])['code'],'FORBIDDEN')
 def test_changed_policy_during_read_is_denied(self):
  from app.client_material import read_bytes as actual
  def reader(root,name,limit):
   r=actual(root,name,limit)
   if name=='a.jsonl':self.p['enabled']=False;self.write_policy()
   return r
  with patch('app.client_history.read_bytes',side_effect=reader):self.assertEqual(answer(self.rt,self.env,[])['code'],'FORBIDDEN')
 def test_torn_and_linked_exports_fail_closed(self):
  (self.exports/'a.jsonl').write_text('{"role":"assistant"}')
  self.assertEqual(answer(self.rt,self.env,[])['code'],'HISTORY_UNAVAILABLE')
  os.link(self.exports/'a.jsonl',self.exports/'other.jsonl');self.assertEqual(answer(self.rt,self.env,[])['code'],'HISTORY_UNAVAILABLE')
 def test_native_state_stale_or_invalid_is_not_live_progress(self):
  self.assertEqual(native_status(self.rt)['status'],'not_integrated')
  f=self.rt/'native-work.json';f.write_text(json.dumps({'schema_version':1,'status':'running','observed_at':self.now-200}));r=native_status(self.rt);self.assertEqual(r['status'],'stale');self.assertIsNone(r['progress'])
  f.write_text('{"schema_version":1,"status":"running","observed_at":NaN}');self.assertEqual(native_status(self.rt)['status'],'unknown')
 def test_published_record_preserves_human_clock(self):
  folder=self.rt/'project-records';folder.mkdir();r=dict(schema_version=1,project_id='demo',source_id='fixture',updated_at='2026-10-08T12:00:00+00:00',summary='test',completed=[],blockers=[],evidence=[])
  (folder/'demo.json').write_text(json.dumps(r));(folder/'demo.meta.json').write_text(json.dumps({'observed_at':'2026-10-09T03:00:00+00:00','confirmed_at':'2026-10-01T00:00:00+00:00'}))
  code,data=read_progress({'agent_id':'win-1','projects':['demo']},{'project_id':'demo'},self.rt);self.assertEqual(code,'OK');self.assertEqual(data['narrative_clock'],'confirmed_at');self.assertEqual(data['narrative_freshness'],'expired')
  (folder/'demo.meta.json').write_text('{broken');code,data=read_progress({'agent_id':'win-1','projects':['demo']},{'project_id':'demo'},self.rt);self.assertEqual(data['metadata_state'],'invalid');self.assertIsNone(data['confirmed_at'])
if __name__=='__main__':unittest.main()
