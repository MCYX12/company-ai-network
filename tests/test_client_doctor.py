import tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from app.client_doctor import diagnose
class DoctorTests(unittest.IsolatedAsyncioTestCase):
 async def test_missing_credentials_do_not_attempt_connection(self):
  with tempfile.TemporaryDirectory() as tmp,patch('app.client_doctor.RUNTIME',Path(tmp)),patch.dict('os.environ',{'NATS_URL':'tls://localhost:14222'}),patch('app.client_doctor.connect',new_callable=AsyncMock) as connect:
   r=await diagnose('test');self.assertEqual(r['checks'][0]['status'],'error');connect.assert_not_called()
 async def test_invalid_endpoint_does_not_leak_secret(self):
  with patch.dict('os.environ',{'NATS_URL':'tls://user:very-secret@localhost:14222'}):
   r=await diagnose('test');self.assertNotIn('very-secret',str(r));self.assertEqual(r['checks'][0]['stage'],'configuration')
