import hashlib,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tests.helpers.client import load_client_worker
from tests.test_client_upgrade import mod as upgrade
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('worker_doctor_test',ROOT/'packaging/client/worker_doctor.py');doctor=importlib.util.module_from_spec(spec)
with patch.dict(sys.modules,{'worker':load_client_worker(),'upgrade':upgrade}):spec.loader.exec_module(doctor)
class Tests(unittest.TestCase):
 def test_integrity_detects_tamper_and_ignores_retained_backup_and_identity(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'worker.py').write_bytes(b'code');(root/'client.json').write_text('LOCAL SECRET')
   inv={'schema_version':1,'package_sha256':None,'files':{'worker.py':hashlib.sha256(b'code').hexdigest()}}
   (root/'installed-manifest.json').write_text(json.dumps(inv));(root/'worker.before.py').write_text('backup')
   self.assertEqual(doctor.integrity(root)['status'],'ok')
   (root/'worker.py').write_text('tampered');r=doctor.integrity(root);self.assertEqual(r['status'],'error');self.assertEqual(r['changed'],['worker.py']);self.assertNotIn('LOCAL SECRET',str(r))
 def test_enabled_history_without_valid_policy_is_error(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'.runtime').mkdir()
   with patch.object(doctor.worker,'settings',return_value={'notes_mode':'raw','history_exports_enabled':True}),patch.object(doctor.worker,'preflight',return_value=('ready',None)):
    r=doctor.diagnose(root)
   check=next(c for c in r['checks'] if c['stage']=='history_exports');self.assertEqual(check['status'],'error')
 def test_partial_native_interface_is_warning_even_when_session_idle(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'.runtime').mkdir()
   native={'status':'idle','scheduled_tasks':{'status':'unknown','hint':'DESKTOP_SCHEDULE_UNAVAILABLE'}}
   with patch.object(doctor.worker,'settings',return_value={'native_work':{'enabled':True}}),patch.object(doctor.worker,'preflight',return_value=('ready',None)),patch('app.native_work.desktop_status',return_value=native):
    check=next(c for c in doctor.diagnose(root)['checks'] if c['stage']=='native_work')
   self.assertEqual(check['status'],'warning');self.assertEqual(check['hint'],'DESKTOP_SCHEDULE_UNAVAILABLE')
 def test_version_digest_ignores_backup_python_file(self):
  w=load_client_worker()
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);(root/'app').mkdir();(root/'worker.py').write_text('worker');(root/'app/worker_kernel.py').write_text('kernel');(root/'app/peer_client.py').write_text('client')
   with patch.object(w,'__file__',str(root/'worker.py')),patch.object(w,'KERNEL_FILE',root/'app/worker_kernel.py'):
    first=w.install_digest();(root/'app/peer_client.before-utf8.py').write_text('old');self.assertEqual(w.install_digest(),first)
    (root/'app/peer_client.py').write_text('changed');self.assertNotEqual(w.install_digest(),first)
