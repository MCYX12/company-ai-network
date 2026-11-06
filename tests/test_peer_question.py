import json,time,unittest
from unittest.mock import patch
from app.peer_question import build_prompt,execute_question
from app.policy import allowed
from app.registry import validate_registry
from app.common import request
from jsonschema import Draft202012Validator
from pathlib import Path
class FakeBudget:
    limit=1000
    def allow(self,*a,**k):return True
    def spend(self,*a,**k):return 1
    def record_denial(self,*a,**k):pass
class PeerTests(unittest.TestCase):
    row={'agent_id':'mac-codex-01','tool':'codex','projects':['shared','private']}
    def test_context_only_router_granted_projects(self):
        with patch('app.peer_question.read_progress',return_value=('DATA_UNAVAILABLE',None)) as read:
            prompt,sources=build_prompt(self.row,{'question':'项目进度如何？','projects':['shared']})
            self.assertEqual(read.call_count,1);self.assertEqual(read.call_args.args[1],{'project_id':'shared'});self.assertNotIn('private',prompt);self.assertEqual(sources,[])
    def test_unknown_project_rejected_without_model(self):
        with patch('app.peer_question.execute_text') as run:
            self.assertEqual(execute_question(self.row,{'question':'hi','projects':['outside']},time.time()+30)[0],'INVALID_REQUEST');run.assert_not_called()
    @patch('app.policy.approved', return_value=True)
    def test_current_grant_removal_denies_stored_payload(self, approval):
        rows={'a':{'enabled':True,'projects':['shared']},'b':{'enabled':True,'projects':['shared','private'],'tool':'codex'}}
        self.assertTrue(allowed('a','b','ask_peer',{'projects':['shared']},rows));self.assertFalse(allowed('a','b','ask_peer',{'projects':['private']},rows));rows['a']['projects']=[];self.assertFalse(allowed('a','b','ask_peer',{'projects':['shared']},rows))
    def test_schema_cannot_supply_grants_or_override_command(self):
        v=Draft202012Validator(json.loads(Path('schemas/query.json').read_text()));b=request();b['action']='submit_task';b['payload']={'operation':'ask_peer','question':'进展？','ttl_seconds':120,'idempotency_key':'peer'}
        self.assertTrue(v.is_valid(b));b['payload']['projects']=['private'];self.assertFalse(v.is_valid(b));b['payload'].pop('projects');b['payload']['prompt']='x';self.assertFalse(v.is_valid(b))
    def test_answer_provenance_from_remote_model(self):
        with patch('app.peer_question.read_progress',return_value=('DATA_UNAVAILABLE',None)),patch('app.peer_question.execute_text',return_value=('OK',{'text':'请问哪个项目？','agent_id':'mac-codex-01'})),patch('app.model_budget.ModelBudget',return_value=FakeBudget()):
            code,data=execute_question(self.row,{'question':'那个项目？','projects':['shared']},time.time()+30)
            self.assertEqual(code,'OK');self.assertEqual(data['source'],'remote_agent_answer');self.assertEqual(data['session_mode'],'new_bounded_question')
if __name__=='__main__':unittest.main()
