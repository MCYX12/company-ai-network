import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.accounts import Accounts

class AccountTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.a=Accounts(Path(self.tmp.name)/'accounts.sqlite3');self.code=self.a.setup_code().read_text();self.a.register_admin(self.code,'owner','test-password-for-owner');self.token=self.a.login('owner','test-password-for-owner')
 def tearDown(self):self.tmp.cleanup()
 def test_admin_setup_single_use_and_login(self):
  self.assertEqual(self.a.who(self.token),'owner');self.assertFalse(self.a.setup_code().exists())
  with self.assertRaises(ValueError):self.a.register_admin(self.code,'other','test-password-for-other')
  with self.assertRaises(ValueError):self.a.login('owner','wrong-password')
  self.a.logout(self.token);self.assertIsNone(self.a.who(self.token))
 def test_invited_admin_and_disable_revokes_session(self):
  invite=self.a.invite('owner','admin',[]);self.a.register_admin(invite['code'],'second','test-password-for-second');token=self.a.login('second','test-password-for-second')
  self.a.admin_status('owner','second',False);self.assertIsNone(self.a.who(token))
  with self.assertRaises(ValueError):self.a.admin_status('owner','owner',False)
 def test_member_registration_single_use_and_no_admin_escalation(self):
  inv=self.a.invite('owner','member',['demo-project']);body=dict(version=1,code=inv['code'],name='测试成员',device='Mac')
  with self.assertRaises(ValueError):self.a.register_admin(inv['code'],'intruder','long-enough-password')
  ident=self.a.import_member('owner',body);self.assertEqual(self.a.listing()['members'][0]['state'],'pending')
  with self.assertRaises(ValueError):self.a.import_member('owner',body)
  self.a.status('owner',ident,'active');self.a.status('owner',ident,'disabled');self.assertEqual(self.a.listing()['members'][0]['state'],'disabled')
 def test_expired_invite_and_session(self):
  inv=self.a.invite('owner','member',['demo-project'])
  with patch('app.accounts.time.time',return_value=time.time()+86401):
   self.assertIsNone(self.a.who(self.token))
   with self.assertRaises(ValueError):self.a.import_member('owner',dict(version=1,code=inv['code'],name='n',device='d'))
 def test_disabled_member_registry_gate(self):
  import app.registry as registry
  import app.iam as iam
  inv=self.a.invite('owner','member',['demo-project']);ident=self.a.import_member('owner',dict(version=1,code=inv['code'],name='n',device='d'))
  root=Path(self.tmp.name);(root/'registry.json').write_text(json.dumps({'schema_version':1,'agents':[dict(agent_id=ident,employee_id=ident,device_id='device',tool='fixture',projects=['demo-project'],enabled=True)]}))
  # Isolate the IAM authority too: a fresh clone has no .runtime, and an unreadable
  # authority correctly fails closed, which would mask the registry overlay under test.
  with patch.object(registry,'RUNTIME',root),patch.object(registry,'registry_path',return_value=root/'registry.json'),patch.object(iam,'RUNTIME',root):
   self.assertFalse(registry.load_registry()[ident]['enabled']);self.a.status('owner',ident,'active');self.assertTrue(registry.load_registry()[ident]['enabled']);self.a.status('owner',ident,'disabled');self.assertFalse(registry.load_registry()[ident]['enabled'])
 def test_missing_account_database_denies_managed_identity(self):
  import app.registry as registry
  root=Path(self.tmp.name)/'missing';root.mkdir()
  path=root/'registry.json';path.write_text(json.dumps({'schema_version':1,'agents':[dict(agent_id='member-test',employee_id='member-test',device_id='device',tool='fixture',projects=['demo-project'],enabled=True)]}))
  with patch.object(registry,'RUNTIME',root),patch.object(registry,'registry_path',return_value=path):self.assertFalse(registry.load_registry()['member-test']['enabled'])
 def test_password_change_invalidates_sessions(self):
  self.a.change_password('owner','test-password-for-owner','new-password-for-owner')
  self.assertIsNone(self.a.who(self.token))
  with self.assertRaises(ValueError):self.a.login('owner','test-password-for-owner')
  self.assertEqual(self.a.who(self.a.login('owner','new-password-for-owner')),'owner')
 def test_login_rate_limit(self):
  for _ in range(10):
   try:self.a.login('owner','wrong-password')
   except ValueError:pass
  with self.assertRaises(ValueError):self.a.login('owner','test-password-for-owner')

class AccountHTTPTests(unittest.TestCase):
 def test_login_gate_cookie_logout(self):
  from app.dashboard import DashboardServer
  from test_dashboard import FakeBridge
  import threading,http.client
  with tempfile.TemporaryDirectory() as tmp:
   a=Accounts(Path(tmp)/'accounts.sqlite3');code=a.setup_code().read_text();a.register_admin(code,'owner','test-password-for-owner')
   server=DashboardServer(('127.0.0.1',0),FakeBridge(),admin=True,accounts=a);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   def call(path,body,cookie=''):
    c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('POST',path,json.dumps(body),{'Content-Type':'application/json','X-Dashboard-Token':server.token,'Cookie':cookie});r=c.getresponse();data=r.read();h=r.getheader('Set-Cookie');c.close();return r.status,data,h
   try:
    self.assertEqual(call('/api/admin/access',{})[0],401)
    status,_,cookie=call('/api/auth/login',dict(name='owner',password='test-password-for-owner'));self.assertEqual(status,200);self.assertIn('HttpOnly',cookie);self.assertIn('SameSite=Strict',cookie)
    self.assertEqual(call('/api/auth/state',{},cookie)[0],200)
    call('/api/auth/logout',{},cookie);self.assertEqual(call('/api/admin/access',{},cookie)[0],401)
   finally:server.shutdown();server.server_close();thread.join()
