import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.local_progress import ensure_report,cache_path,notes_allowed
from scripts.summarize_local_project import collect
class FakeBudget:
 def __init__(self,*a,**k):self.limit=1000
 def allow(self,*a,**k):return True
 def spend(self,*a,**k):return 1
 def record_denial(self,*a,**k):pass
class ProjectSummaryTests(unittest.TestCase):
 def test_history_only_never_reads_workspace_or_runs_git(self):
  with tempfile.TemporaryDirectory() as d,patch('scripts.summarize_local_project.subprocess.run') as git:
   root=Path(d);history=root/'history';history.mkdir();project=root/'private';project.mkdir();(project/'README.md').write_text('PRIVATE FILE NOT AUTHORIZED')
   evidence,info=collect(project,history)
   self.assertEqual(evidence,[]);git.assert_not_called();self.assertEqual(info['reason'],'project_files_not_authorized')
 def test_separate_caches_refresh_and_reuse(self):
  with tempfile.TemporaryDirectory() as d,patch('app.local_progress.RUNTIME',Path(d)),patch('app.claude_history.settings',return_value={'enabled':True}):
   def generate(caller,target,project,deadline,source_version):
    return dict(project=Path(project).name,project_path=project,caller=caller,target=target,expires=time.time()+30,source_version=source_version,notes_allowed=False,generated_at=time.time(),local_model='local',raw_uploaded=False,facts=[{'summary':project}],git={},evidence=[],limitations=[])
   with patch('scripts.summarize_local_project.generate',side_effect=generate) as gen:
    row={'kind':'project_activity','project_path':'/a','last_activity':'t1'}
    a=ensure_report('u','b',row,time.time()+10);again=ensure_report('u','b',row,time.time()+10)
    self.assertEqual(a,again);self.assertEqual(gen.call_count,1)
    other=ensure_report('u','b',{**row,'project_path':'/b'},time.time()+10);self.assertNotEqual(a['project'],other['project']);self.assertEqual(gen.call_count,2)
    ensure_report('u','b',{**row,'last_activity':'t2'},time.time()+10);self.assertEqual(gen.call_count,3)
    p=cache_path('u','b','/a');obj=json.loads(p.read_text());obj['expires']=0;p.write_text(json.dumps(obj));ensure_report('u','b',{**row,'last_activity':'t2'},time.time()+10);self.assertEqual(gen.call_count,4)
    self.assertNotEqual(cache_path('other','b','/a'),cache_path('u','b','/a'))
 def test_notes_scope_exact_match_expiry(self):
  with tempfile.TemporaryDirectory() as d,patch('app.local_progress.RUNTIME',Path(d)):
   p=Path(d)/'project-notes-access.json';p.write_text(json.dumps({'grants':[dict(caller='u',target='b',root='/a',expires=time.time()+60,enabled=True)]}));p.chmod(0o600)
   self.assertTrue(notes_allowed('u','b','/a'));self.assertFalse(notes_allowed('u','b','/a/child'));self.assertFalse(notes_allowed('other','b','/a'))
 def test_question_case_space_and_separator_matching(self):
  from app.claude_history import answer
  row=dict(name='Qwen mac',kind='project_activity',project_path='/Qwen mac',sessions=1,last_activity='today',topics=[],read_requests=0,edit_requests=0,progress_signals=[])
  with patch('app.claude_history.settings',return_value={'root':'/history','mode':'local_only'}),patch('app.claude_history.scan',return_value=([row],{'limited':False,'skipped':0})),patch('app.claude_history.ensure_report',return_value=None) as gen:
   for question in ['QWEN MAC 进展','qwen-mac进展','qwen_mac进展']:
    answer({'agent_id':'b','tool':'claude-code'},{'caller':'u','question':question},time.time()+60)
   self.assertEqual(gen.call_count,3)

 def test_ambiguous_description_considers_project_summaries(self):
  from app.claude_history import answer
  def row(name):return dict(name=name,kind='project_activity',project_path='/'+name,sessions=1,last_activity='today',topics=[],read_requests=0,edit_requests=0,progress_signals=[])
  with patch('app.claude_history.settings',return_value={'root':'/history','mode':'local_only'}),patch('app.claude_history.scan',return_value=([row('alpha'),row('beta')],{'limited':False,'skipped':0})),patch('app.claude_history.ensure_report',return_value=None) as gen:
   answer({'agent_id':'b','tool':'claude-code'},{'caller':'u','question':'企业工作台进度'},time.time()+110)
   self.assertEqual(gen.call_count,2)

 def test_chinese_description_retrieves_relevant_history(self):
  from app.claude_history import scan
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)
   for name,text,stamp in [('alpha','这是企业工作台，管理成员权限','2026-01-01T00:00:00Z'),('beta','这是歌词创作工具','2026-09-29T00:00:00Z')]:
    (root/(name+'.jsonl')).write_text(json.dumps({'type':'assistant','cwd':'/projects/'+name,'timestamp':stamp,'message':{'content':text}}))
   rows,_=scan(root,'问企业工作台最近有什么进展')
   self.assertEqual(rows[0]['name'],'alpha')

 def test_expired_history_is_explained_not_project_missing(self):
  from app.peer_question import execute_question
  from app.claude_history import HistoryAccessError
  with patch('app.claude_history.answer',side_effect=HistoryAccessError('HISTORY_ACCESS_EXPIRED')),patch('app.model_budget.ModelBudget',FakeBudget):
   code,data=execute_question({'agent_id':'b','tool':'claude-code','projects':['p']},{'caller':'u','projects':['p'],'question':'项目进展'},time.time()+30)
   self.assertEqual(code,'INVALID_REQUEST');self.assertEqual(data['hint'],'HISTORY_ACCESS_EXPIRED')
