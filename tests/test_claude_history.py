import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.claude_history import scan,answer
class HistoryTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name).resolve();self.f=self.root/'session.jsonl';self.marker='SECRET_RAW_TRANSCRIPT_NOT_FOR_MODEL'
  self.f.write_text('\n'.join(json.dumps(r) for r in [dict(type='user',cwd='/projects/website',timestamp='2026-09-28T00:00:00Z',message={'content':self.marker+' 前端'}),dict(type='assistant',cwd='/projects/website',message={'content':[{'type':'tool_use','name':'Write','input':{'content':self.marker}}]})]))
 def tearDown(self):self.t.cleanup()
 def test_index_never_returns_raw_text(self):
  rows,coverage=scan(self.root,'website');self.assertEqual(rows[0]['name'],'website');self.assertEqual(rows[0]['edit_requests'],1);self.assertNotIn(self.marker,str(rows));self.assertEqual(coverage['files'],1)
 def test_review_receives_only_aggregate(self):
  c=dict(root=str(self.root),mode='local_report_review')
  with patch('app.claude_history.settings',return_value=c),patch('app.text_task.execute_text',return_value=('OK',{'text':'不能判断是否完成'})) as model:
   code,data=answer({'agent_id':'b','tool':'claude-code'},{'caller':'a','question':'进展？'},time.time()+60)
   self.assertEqual(code,'OK');self.assertNotIn(self.marker,model.call_args.args[1]['prompt']);self.assertNotIn('/projects/',model.call_args.args[1]['prompt']);self.assertFalse(data['history_uploaded']);self.assertTrue(data['model_invoked'])
 def test_local_only_does_not_invoke_model(self):
  with patch('app.claude_history.settings',return_value=dict(root=str(self.root),mode='local_only')),patch('app.text_task.execute_text') as model:
   code,data=answer({'agent_id':'b','tool':'claude-code'},{'caller':'a','question':'进展？'});model.assert_not_called();self.assertFalse(data['model_invoked'])
 def test_symlink_transcript_not_read(self):
  self.f.unlink();self.f.symlink_to('/etc/passwd');rows,coverage=scan(self.root,'x');self.assertEqual(rows,[]);self.assertEqual(coverage['skipped'],1)

 def test_changed_cwd_in_one_session_keeps_both_projects(self):
  with self.f.open('a') as f:f.write('\n'+json.dumps(dict(type='user',cwd='/projects/second',message={'content':'next'})))
  rows,_=scan(self.root,'second');self.assertEqual({r['name'] for r in rows},{'website','second'});self.assertTrue(all(r['sessions']==1 for r in rows))
 def test_home_is_general_session(self):
  self.f.write_text(json.dumps(dict(type='user',cwd=str(Path.home()),message={'content':'test'})))
  rows,_=scan(self.root,'x');self.assertEqual(rows[0]['kind'],'general_session')
 def test_failed_review_reports_the_cli_diagnostic(self):
  c=dict(root=str(self.root),mode='local_report_review')
  failure=('EXECUTION_FAILED',{'tool':'claude-code','exit_code':1,
      'diagnostic':'API Error: API returned an empty or malformed response (HTTP 200)','hint':'check login'})
  with patch('app.claude_history.settings',return_value=c),patch('app.text_task.execute_text',return_value=failure):
   code,data=answer({'agent_id':'b','tool':'claude-code'},{'caller':'a','question':'进展？'},time.time()+60)
  self.assertEqual(code,'OK')
  self.assertEqual(data['review_status'],'EXECUTION_FAILED')
  self.assertEqual(data['review_detail'],{'tool':'claude-code','exit_code':1,
      'diagnostic':'API Error: API returned an empty or malformed response (HTTP 200)'})
  self.assertIn('复核未成功',data['text'])
  self.assertFalse(data['model_invoked'])
 def test_failed_review_without_detail_still_states_the_code(self):
  c=dict(root=str(self.root),mode='local_report_review')
  with patch('app.claude_history.settings',return_value=c),patch('app.text_task.execute_text',return_value=('OUTPUT_TOO_LARGE',None)):
   code,data=answer({'agent_id':'b','tool':'claude-code'},{'caller':'a','question':'进展？'},time.time()+60)
  self.assertEqual(code,'OK')
  self.assertEqual(data['review_status'],'OUTPUT_TOO_LARGE')
  self.assertNotIn('review_detail',data)
 def test_ungranted_project_does_not_scan_history(self):
  from app.peer_question import execute_question
  with patch('app.claude_history.scan') as scanner:
   code,_=execute_question({'agent_id':'b','tool':'claude-code','projects':['allowed']},{'caller':'a','projects':['other'],'question':'x'},time.time()+60)
   self.assertEqual(code,'INVALID_REQUEST');scanner.assert_not_called()
