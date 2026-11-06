"""Production gate cannot accept local evidence, stale sources or modified field files."""
import copy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io

from scripts import enterprise_acceptance as acceptance

class AcceptanceTests(unittest.TestCase):
    def test_invalid_measurement_ranges_and_truthy_descriptors_block(self):
        cases=[('E-07','success_ratio',1.01),('E-07','connections',1000.5),
               ('E-02','routers',2.5),('E-02','physical_failure_domains',3.5),
               ('E-11','employees',50.5),('E-08','receiver_receipt',True),
               ('E-08','receiver_receipt',{'fake':'receipt'})]
        for item,key,value in cases:
            with self.subTest(item=item,key=key,value=value):
                original=copy.deepcopy(self.data)
                self.data['items'][item]['metrics'][key]=value
                self.assertFalse(self.verify()['ok'])
                self.data=original
        for site in (True, ['site'], {'site':'x'}, '   '):
            self.data['environment']['site']=site
            self.assertFalse(self.verify()['ok'])
    def test_boolean_format_wrong_role_and_stale_signoffs_refused(self):
        for mutate in (
            lambda data:data.update(format=True),
            lambda data:data['items']['E-01'].update(responsible_role='unrelated'),
            lambda data:data['signoffs']['business'].update(name=True),
            lambda data:data['signoffs']['security'].update(accepted_at='not a date'),
            lambda data:data['signoffs']['operations'].update(accepted_at=(self.now-timedelta(days=31)).isoformat()),
            lambda data:data['signoffs']['operations'].update(accepted_at=(self.now+timedelta(days=1)).isoformat()),
        ):
            data=copy.deepcopy(self.data);mutate(data);self.path.write_text(json.dumps(data))
            self.assertFalse(acceptance.verify(self.path,self.root,self.now)['ok'])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'app').mkdir();(self.root/'app/code.py').write_text('source')
        self.path=self.root/'acceptance.json';self.now=datetime.now(timezone.utc)
        proof=self.root/'proof.json';proof.write_text('{"test_fixture_only":true}')
        self.data=acceptance.template();self.data['source_digest']=acceptance.source_digest(self.root)
        self.data['environment']['site']='fixture-site'
        for row in self.data['items'].values():
            row.update(owner='fixture-owner',status='accepted',accepted_at=self.now.isoformat(),evidence=[{'path':proof.name,'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}])
        self.data['items']['E-02']['metrics']={'physical_failure_domains':3,'routers':2,'partition_revocation_passed':True}
        self.data['items']['E-03']['metrics']={'real_model':True,'public_network_blocked':True,'cold_start_passed':True}
        self.data['items']['E-07']['metrics']={'connections':1000,'qps':100,'duration_seconds':1800,'success_ratio':.999,'p95_ms':500}
        self.data['items']['E-08']['metrics']={'real_channel':True,'receiver_receipt':'fixture'}
        self.data['items']['E-09']['metrics']={'independent_storage':True,'immutable_retention':True}
        self.data['items']['E-10']['metrics']={'rto_seconds':10,'rpo_seconds':10,'rto_target_seconds':60,'rpo_target_seconds':60,'encrypted':True,'offsite':True,'complete_restore':True}
        self.data['items']['E-11']['metrics']={'employees':50}
        self.data['signoffs']={role:{'name':'fixture-person','accepted_at':self.now.isoformat()} for role in ('business','security','operations')}
    def tearDown(self):self.tmp.cleanup()
    def verify(self):
        self.path.write_text(json.dumps(self.data))
        return acceptance.verify(self.path,self.root,self.now)
    def test_positive_descriptor_fixture(self):self.assertTrue(self.verify()['ok'])
    def test_unfilled_template_fails(self):
        self.data=acceptance.template();self.assertFalse(self.verify()['ok'])
    def test_local_drill_cannot_replace_field_acceptance(self):
        self.data['items']['E-02']['evidence_level']='隔离验证';self.assertFalse(self.verify()['ok'])
    def test_source_change_invalidates_acceptance(self):
        (self.root/'app/code.py').write_text('changed');self.assertFalse(self.verify()['ok'])
    def test_proof_tampering_blocks(self):
        (self.root/'proof.json').write_text('changed');self.assertFalse(self.verify()['ok'])
    def test_expired_acceptance_blocks(self):
        self.data['items']['E-01']['accepted_at']=(self.now-timedelta(days=31)).isoformat();self.assertFalse(self.verify()['ok'])
    def test_capacity_shape_and_omitted_signoff_block(self):
        self.data['items']['E-07']['metrics']['qps']=50
        self.data['signoffs']['security']=None;self.assertFalse(self.verify()['ok'])
    def test_bad_metric_types_fail_closed(self):
        self.data['items']['E-02']['metrics']['routers']='two';self.assertFalse(self.verify()['ok'])
    def test_path_escape_blocks(self):
        self.data['items']['E-01']['evidence'][0]['path']='../secret';self.assertFalse(self.verify()['ok'])
    def test_invalid_manifest_type_blocks(self):
        self.path.write_text('[]');self.assertFalse(acceptance.verify(self.path,self.root,self.now)['ok'])

    def test_production_cli_ok_agrees_with_field_failure(self):
        from scripts import verify_release
        self.data['items']['E-01']['status']='pending';self.verify()
        source={'ok':True,'problems':[],'source_matches_release':True,'source_content_matches':True}
        out=io.StringIO()
        with patch.object(verify_release,'verify',return_value=source), patch('sys.argv',['verify_release','--root',str(self.root),'--production','--acceptance-manifest',str(self.path)]), contextlib.redirect_stdout(out):
            self.assertEqual(verify_release.main(),1)
        report=json.loads(out.getvalue())
        self.assertFalse(report['ok']);self.assertFalse(report['production_ready'])
        self.assertTrue(report['source_release_ok'])

    def test_production_cli_positive_descriptor_fixture(self):
        from scripts import verify_release
        self.verify()
        source={'ok':True,'problems':[],'source_matches_release':True,'source_content_matches':True}
        out=io.StringIO()
        with patch.object(verify_release,'verify',return_value=source), patch('sys.argv',['verify_release','--root',str(self.root),'--production','--acceptance-manifest',str(self.path)]), contextlib.redirect_stdout(out):
            self.assertEqual(verify_release.main(),0)
        self.assertTrue(json.loads(out.getvalue())['production_ready'])

if __name__=='__main__':unittest.main()
