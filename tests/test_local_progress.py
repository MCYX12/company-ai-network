import unittest,tempfile,json,time
from pathlib import Path
from unittest.mock import patch
from app.local_progress import report_for
from scripts.summarize_local_project import guard_facts
from app.history_progress import signals
class ProgressTests(unittest.TestCase):
 def test_raw_passage_blocked(self):
  text='这是足够长的一段非常私密的历史记录不应该外发给模型'
  with self.assertRaises(ValueError):guard_facts([{'summary':text,'basis':'historical_claim','evidence_ids':['E1']}],[{'id':'E1','kind':'historical_assistant','text':text}])
 def test_evidence_type_enforced(self):
  with self.assertRaises(ValueError):guard_facts([{'summary':'已经开发','basis':'documented','evidence_ids':['E1']}],[{'id':'E1','kind':'historical_user','text':'请开发'}])
 def test_missing_evidence_blocked(self):
  with self.assertRaises(ValueError):guard_facts([{'summary':'已运行','basis':'historical_claim','evidence_ids':['wrong']}],[])
 def test_negation_not_completed(self):
  self.assertEqual(signals('模型训练尚未完成','assistant')[0]['state'],'历史计划或未完成')
 def test_report_binding_and_expiration(self):
  with tempfile.TemporaryDirectory() as d,patch('app.local_progress.RUNTIME',Path(d)):
   p=Path(d)/'local-progress-report.json';r=dict(caller='a',target='b',project_path='/p',expires=time.time()+60,project='p',generated_at=0,local_model='local',raw_uploaded=False,notes_allowed=False,facts=[],git={},evidence=[],limitations=[]);p.write_text(json.dumps(r));p.chmod(0o600)
   self.assertIsNone(report_for('other','b','/p'));self.assertIsNotNone(report_for('a','b','/p'));r['expires']=0;p.write_text(json.dumps(r));self.assertIsNone(report_for('a','b','/p'))

 def test_user_instruction_not_historical_fact(self):
  with self.assertRaises(ValueError):guard_facts([{'summary':'完成开发','basis':'historical_claim','evidence_ids':['E1']}],[{'id':'E1','kind':'historical_user','text':'请帮我开发'}])
