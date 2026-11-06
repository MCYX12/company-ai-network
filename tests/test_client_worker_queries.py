import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('query_worker',ROOT/'packaging/client/worker.py')
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


class WorkerQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config={'command':['missing-model'],'capabilities':['get_status','get_project_progress'],'projects':['demo-project']}
        self.body={'target_agent_id':'windows-test','expires_at':time.time()+60,'action':'get_project_progress','payload':{'project_id':'demo-project'}}
    def tearDown(self):self.temp.cleanup()
    def call(self):return worker.query_result(self.config,'windows-test',self.body,self.root)
    def test_queries_do_not_call_model_and_preserve_provenance(self):
        folder=self.root/'.runtime/project-records';folder.mkdir(parents=True)
        record={'schema_version':1,'project_id':'demo-project','source_id':'published/fixture','updated_at':datetime.now(timezone.utc).isoformat(),'summary':'生成测试资料','completed':['测试项'],'blockers':['待现场确认'],'evidence':['fixture-proof']}
        (folder/'demo-project.json').write_text(json.dumps(record))
        with patch.object(worker,'run_command') as model:
            code,data=self.call();self.assertEqual(code,'OK');self.assertIsNone(data['progress']);self.assertEqual(data['source'],'published_project_record');model.assert_not_called()
    def test_missing_and_cross_project_are_explicit(self):
        self.assertEqual(self.call()[0],'DATA_UNAVAILABLE')
        for project in ['private-project','../demo-project']:
            self.body['payload']['project_id']=project;self.assertEqual(self.call()[0],'FORBIDDEN')
    def test_wrong_identity_expired_and_unsupported_are_rejected(self):
        self.body['target_agent_id']='other';self.assertEqual(self.call()[0],'FORBIDDEN')
        self.body['target_agent_id']='windows-test';self.body['expires_at']=time.time()-1;self.assertEqual(self.call()[0],'EXPIRED')
        self.body['expires_at']=time.time()+60;self.body['action']='ask_peer';self.assertEqual(self.call()[0],'UNSUPPORTED_ACTION')
    def test_status_reports_real_command_preflight(self):
        self.body['action']='get_status'
        with patch.object(worker,'run_command') as model:
            code,data=self.call();self.assertEqual(code,'OK');self.assertEqual(data['adapter_status'],'not_found');self.assertFalse(data['synthetic']);model.assert_not_called()
