import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app.admin_access import handle
from app.conversation_access import AccessStore
class AdminTests(unittest.TestCase):
 def test_approve_revoke_and_stale_decision(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=AccessStore(Path(tmp)/'db');r=s.request('a','b',['p'])
   reg={x:dict(enabled=True,projects=['p'],employee_id='employee',device_id='device') for x in ['a','b']}
   with patch('app.admin_access.AccessStore',return_value=s),patch('app.admin_access.load_registry',return_value=reg):
    body=dict(id=r['approval_id'],decision='approved',seconds=60,reason='test')
    handle(body);self.assertTrue(s.permits('a','b',['p']))
    with self.assertRaises(ValueError):handle(body)
    body['decision']='revoked';handle(body);self.assertFalse(s.permits('a','b',['p']))
 def test_no_expiry_is_allowed_and_is_the_cli_default(self):
  from app.access_admin import build_parser
  self.assertEqual(build_parser().parse_args(['list']).seconds,0)
  with tempfile.TemporaryDirectory() as tmp:
   s=AccessStore(Path(tmp)/'db');r=s.request('a','b',['p'])
   reg={x:dict(enabled=True,projects=['p'],employee_id='employee',device_id='device') for x in ['a','b']}
   with patch('app.admin_access.AccessStore',return_value=s),patch('app.admin_access.load_registry',return_value=reg):
    handle(dict(id=r['approval_id'],decision='approved',seconds=0,reason='standing'))
    self.assertTrue(s.permits('a','b',['p']))
    import time
    with patch('app.conversation_access.time.time',return_value=time.time()+50*365*86400):
     self.assertTrue(s.permits('a','b',['p']))

 def test_scope_change_and_invalid_input(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=AccessStore(Path(tmp)/'db');r=s.request('a','b',['p'])
   with patch('app.admin_access.AccessStore',return_value=s),patch('app.admin_access.load_registry',return_value={}):
    body=dict(id=r['approval_id'],decision='approved',seconds=60,reason='test')
    with self.assertRaises(ValueError):handle(body)
    body['seconds']=True
    with self.assertRaises(ValueError):handle(body)
    self.assertFalse(s.permits('a','b',['p']))
