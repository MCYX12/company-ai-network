import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from scripts.revoke_device import candidate,revoke
from app.policy import allowed

class RevokeTests(unittest.TestCase):
    def setUp(self):
        self.registry={'schema_version':1,'agents':[dict(agent_id=a,device_id=d,employee_id='employee',tool='fixture',projects=['demo-project'],enabled=True) for a,d in [('ag-a','server-device'),('ag-b','mac-device'),('ag-d','mac-device')]]}
        self.config={'accounts':{'POC':{'users':[{'user':a} for a in ['router','ag-a','ag-b','ag-d']]}}}
    def test_all_device_identities_disabled_and_removed(self):
        reg,config,ids=candidate(self.registry,self.config,'mac-device')
        self.assertEqual(ids,['ag-b','ag-d']);self.assertEqual([u['user'] for u in config['accounts']['POC']['users']],['router','ag-a'])
        rows={r['agent_id']:r for r in reg['agents']};self.assertFalse(allowed('ag-a','ag-b','get_status',{},rows));self.assertFalse(allowed('ag-b','ag-a','get_status',{},rows))
        self.assertTrue(self.registry['agents'][1]['enabled'])
    def test_unknown_device_rejected(self):
        with self.assertRaises(ValueError):candidate(self.registry,self.config,'missing')
    def test_dry_run_and_validation_failure_preserve_configs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,value in [('registry.json',self.registry),('nats.conf',self.config)]: (root/name).write_text(json.dumps(value))
            before=(root/'registry.json').read_bytes()
            self.assertFalse(revoke(root,'mac-device','test')['applied']);self.assertEqual((root/'registry.json').read_bytes(),before)
            with patch('scripts.revoke_device.subprocess.run',side_effect=RuntimeError('invalid')):
                with self.assertRaises(RuntimeError):revoke(root,'mac-device','test',True)
            self.assertEqual((root/'registry.json').read_bytes(),before)
    def test_backups_and_repeated_apply_are_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,value in [('registry.json',self.registry),('nats.conf',self.config)]: (root/name).write_text(json.dumps(value))
            with patch('scripts.revoke_device.subprocess.run'):
                report=revoke(root,'mac-device','test',True);revoke(root,'mac-device','test',True)
            old=json.loads((Path(report['backup'])/'registry.json').read_text());self.assertTrue(old['agents'][1]['enabled'])
            self.assertEqual((root/'registry.json').stat().st_mode&0o777,0o600)
if __name__=='__main__':unittest.main()
