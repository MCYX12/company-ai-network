import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from scripts import owner_test_acceptance as owner, enterprise_acceptance as production
from scripts.refresh_project_record import publish
from app.project_records import read_progress


class OwnerTestReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        (self.root/'app').mkdir(); (self.root/'app/code.py').write_text('current')
        self.path=self.root/'receipt.json'; self.now=datetime.now(timezone.utc)
        self.data=owner.template(); self.data['source_digest']=owner.source_digest(self.root)
        self.data['environment']['site']='local QA'
        self.data['owner']={'name':'current user','role':'测试负责人','authorization_quote':'I will test the owner signoff','accepted_at':self.now.isoformat()}
        proof=self.root/'proof.json';proof.write_text('{"local_fixture":true}')
        for row in self.data['checks'].values():
            row.update(status='passed',evidence=[{'path':proof.name,'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}])
    def tearDown(self): self.temp.cleanup()
    def verify(self):
        self.path.write_text(json.dumps(self.data)); return owner.verify(self.path,self.root,self.now)
    def test_user_can_test_all_roles_without_production_admission(self):
        result=self.verify();self.assertTrue(result['test_ready']);self.assertIs(result['production_ready'],False)
        self.assertFalse(production.verify(self.path,self.root,self.now)['ok'])
    def test_incomplete_scope_receipt_and_forged_ready_are_refused(self):
        baseline=copy.deepcopy(self.data)
        for change in (lambda d:d.update(scope='production'),lambda d:d.update(production_ready=True),
                       lambda d:d['owner'].update(authorization_quote=''),
                       lambda d:d['owner'].update(accepted_at=(self.now+timedelta(seconds=1)).isoformat()),
                       lambda d:d['owner'].update(accepted_at=(self.now-timedelta(days=31)).isoformat()),
                       lambda d:d['checks']['regression'].update(status='pending')):
            self.data=copy.deepcopy(baseline);change(self.data);self.assertFalse(self.verify()['ok'])
    def test_changed_source_or_proof_blocks(self):
        (self.root/'app/code.py').write_text('changed');self.assertFalse(self.verify()['ok'])
        self.data['source_digest']=owner.source_digest(self.root)
        (self.root/'proof.json').write_text('changed');self.assertFalse(self.verify()['ok'])
    def test_path_escape_and_link_block(self):
        (self.root/'link.json').symlink_to(self.root/'proof.json')
        for path in ('../proof.json','link.json'):
            self.data['checks']['regression']['evidence'][0]['path']=path
            self.assertFalse(self.verify()['ok'])


class RetirementTests(unittest.TestCase):
    def test_retired_source_cannot_be_read_or_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Path(tmp)
            record={'schema_version':1,'project_id':'demo-project','source_id':'fixture','updated_at':datetime.now(timezone.utc).isoformat(),'summary':'old','completed':[],'blockers':[],'evidence':[]}
            publish(record,runtime)
            (runtime/'project-retirements.json').write_text(json.dumps({'format':1,'retired':['demo-project']}))
            code,data=read_progress({'projects':['demo-project']},{'project_id':'demo-project'},runtime)
            self.assertEqual(code,'DATA_UNAVAILABLE')
            from app.project_state import statuses
            self.assertEqual(statuses(runtime),[])
            from app.healthcheck import local_checks
            retired_check=next(c for c in local_checks(runtime) if c['check']=='project_record:demo-project')
            self.assertEqual(retired_check['status'],'fail')
            self.assertEqual(retired_check['detail']['error'],'RETIRED_RECORD_PRESENT')
            with self.assertRaises(ValueError):publish(record,runtime)
    def test_malformed_tombstone_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Path(tmp);(runtime/'project-retirements.json').write_text('null')
            self.assertEqual(read_progress({'projects':['p']},{'project_id':'p'},runtime)[0],'DATA_INVALID')


if __name__=='__main__': unittest.main()
