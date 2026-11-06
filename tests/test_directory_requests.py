import unittest,tempfile,time,json,asyncio
from pathlib import Path
from unittest.mock import patch,AsyncMock
from app.directory_requests import DirectoryRequests,validate_root
from app.directory_context import execute,CURRENT
from app.local_progress import notes_allowed
class DirectoryTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=DirectoryRequests(Path(self.tmp.name)/'d.sqlite3')
 def tearDown(self):self.tmp.cleanup()
 def test_deduplicate_pending_approved_denied(self):
  a=self.store.request('a','b','/Users/mac/new');self.assertEqual(a['id'],self.store.request('a','b','/Users/mac/new')['id'])
  self.store.decide(a['id'],'approved',100,'yes','admin');self.assertEqual(a['id'],self.store.request('a','b','/Users/mac/new')['id'])
  self.store.decide(a['id'],'revoked',100,'stop','admin');self.assertEqual(self.store.request('a','b','/Users/mac/new')['state'],'revoked')
 def test_expiration_creates_one_pending(self):
  a=self.store.request('a','b','/Users/mac/new');self.store.decide(a['id'],'approved',1,'yes','admin')
  with self.store.db() as db:db.execute('UPDATE requests SET expires=0')
  b=self.store.request('a','b','/Users/mac/new');self.assertNotEqual(a['id'],b['id']);self.assertEqual(b['id'],self.store.request('a','b','/Users/mac/new')['id'])
 def test_no_expiry_approval_stays_valid(self):
  r=self.store.request('a','b','/Users/mac/new')
  self.store.decide(r['id'],'approved',0,'standing','admin')
  self.assertTrue(self.store.permits_ids('a','b',[r['id']]))
  far=time.time()+50*365*86400
  with patch('app.directory_requests.time.time',return_value=far):
   self.assertTrue(self.store.permits_ids('a','b',[r['id']]))
   self.assertEqual([x['state'] for x in self.store.listing('a','b')],['approved'])
  other=self.store.request('a','b','/Users/mac/other')
  with self.assertRaises(ValueError):self.store.decide(other['id'],'approved',-1,'bad','admin')

 def test_new_directory_has_no_read_permission_until_approval(self):
  from app.local_progress import RUNTIME
  r=self.store.request('a','b','/Users/mac/new')
  with patch('app.local_progress.RUNTIME',Path(self.tmp.name)):
   def question(row,payload,deadline):return 'OK',{'allowed':notes_allowed('a','b','/Users/mac/new'),'projects':[]}
   with patch('app.peer_question.execute_question',side_effect=question):
    self.assertFalse(execute({'agent_id':'b'},{'caller':'a'},time.time()+30,self.store.dispatch('a','b',time.time()+30))[1]['allowed'])
    self.store.decide(r['id'],'approved',100,'yes','admin')
    self.assertTrue(execute({'agent_id':'b'},{'caller':'a'},time.time()+30,self.store.dispatch('a','b',time.time()+30))[1]['allowed'])
    self.assertFalse(notes_allowed('a','b','/Users/mac/new')) # no grant leaks outside dispatch context
    self.store.decide(r['id'],'revoked',100,'stop','admin')
    self.assertFalse(execute({'agent_id':'b'},{'caller':'a'},time.time()+30,self.store.dispatch('a','b',time.time()+30))[1]['allowed'])
    self.assertFalse(self.store.permits_ids('a','b',[r['id']]))
 def test_target_and_caller_binding(self):
  r=self.store.request('a','b','/Users/mac/new');self.store.decide(r['id'],'approved',100,'yes','admin')
  self.assertEqual(self.store.dispatch('other','b',time.time()+10),[])
  with self.assertRaises(ValueError):execute({'agent_id':'b'},{'caller':'other'},time.time()+30,self.store.dispatch('a','b',time.time()+30))
 def test_roots_and_request_limit(self):
  for p in ['/','/Users/mac','/Users/mac/.ssh','/Users/mac/a/../b']:
   with self.assertRaises(ValueError):validate_root(p)
  for n in range(30):self.store.request('a','b','/Users/mac/p'+str(n))
  with self.assertRaises(ValueError):self.store.request('a','b','/Users/mac/excess')
 def test_router_uses_task_owner_not_result_claim(self):
  from app.router import Router
  from app.common import encode,PREFIX
  from types import SimpleNamespace
  with patch('app.router.RUNTIME',Path(self.tmp.name)),patch('app.directory_requests.RUNTIME',Path(self.tmp.name)),patch('app.router.allowed',return_value=True):
   router=Router(SimpleNamespace(publish=AsyncMock()))
   # Real task store with owner from ingress, target from enrollment.
   row=router.tasks.submit('owner','peer','key','ask_peer',{'caller':'owner','projects':['p'],'question':'x'},120)
   body={'task_id':row['task_id'],'code':'OK','data':{'text':'x','directory_requests':[{'root':'/Users/mac/new'}],'caller':'forged'}}
   asyncio.run(router.task_result(SimpleNamespace(subject=f'{PREFIX}.agent.peer.result',data=encode(body))))
   requests=DirectoryRequests(Path(self.tmp.name)/'directory-requests.sqlite3').listing()
   self.assertEqual(requests[0]['caller'],'owner');self.assertEqual(requests[0]['target'],'peer')

 def test_expired_history_can_request_known_directory_without_reading_history(self):
  from app.claude_history import answer,HistoryAccessError
  row=dict(name='new',project_path='/Users/mac/new',kind='project_activity',sessions=0,last_activity='',text_events=0,read_requests=0,edit_requests=0,tool_requests=0,topics=[],progress_signals=[],score=0,catalog_only=True)
  with patch('app.claude_history.settings',side_effect=HistoryAccessError('HISTORY_ACCESS_EXPIRED')),patch('app.claude_history.known_projects',return_value=[row]),patch('app.claude_history.ensure_report',return_value=None),patch('app.claude_history.notes_allowed',return_value=False),patch('app.claude_history.scan') as scan:
   code,data=answer({'tool':'claude-code','agent_id':'b'},{'caller':'a','question':'new进度'},time.time()+30)
   scan.assert_not_called();self.assertEqual(code,'OK');self.assertEqual(data['directory_requests'],[{'root':'/Users/mac/new'}]);self.assertFalse(data['model_invoked'])
 def test_unknown_history_expiry_does_not_fall_back_to_demo(self):
  from app.claude_history import answer,HistoryAccessError
  with patch('app.claude_history.settings',side_effect=HistoryAccessError('HISTORY_ACCESS_EXPIRED')),patch('app.claude_history.known_projects',return_value=[]):
   with self.assertRaises(HistoryAccessError):answer({'tool':'claude-code','agent_id':'b'},{'caller':'a','question':'new'},time.time()+30)

 def test_rejected_directory_only_reopens_by_admin_action(self):
  r=self.store.request('a','b','/Users/mac/new');self.store.decide(r['id'],'denied',60,'no','admin')
  self.assertEqual(self.store.request('a','b','/Users/mac/new')['state'],'denied')
  self.store.decide(r['id'],'pending',60,'reconsider','admin');self.store.decide(r['id'],'approved',60,'yes','admin')
  self.assertTrue(self.store.permits_ids('a','b',[r['id']]))
 def test_notes_only_generation_does_not_reopen_expired_history(self):
  from scripts.summarize_local_project import generate,MODEL
  from app.claude_history import HistoryAccessError
  evidence=[dict(id='E1',kind='current_project_document',text='Local document about information management.',time=0)]
  result={'response':json.dumps({'facts':[{'summary':'项目说明描述了资料整理用途。','basis':'documented','evidence_ids':['E1']}]})}
  with patch('scripts.summarize_local_project.settings',side_effect=HistoryAccessError('HISTORY_ACCESS_EXPIRED')),patch('app.local_progress.notes_allowed',return_value=True),patch('scripts.summarize_local_project.collect',return_value=(evidence,{'available':True})) as collect,patch('scripts.summarize_local_project.request',side_effect=[{'models':[{'name':MODEL,'size':100000001}]},result]),patch('app.claude_history.scan') as scan:
   report=generate('a','b','/Users/mac/new',time.time()+60,'v')
   scan.assert_not_called();collect.assert_called_once_with(Path('/Users/mac/new'),None,True);self.assertTrue(report['notes_allowed'])
