import importlib.util,sys,json,tempfile,unittest,zipfile,hashlib,sqlite3
from contextlib import closing
from unittest.mock import patch
from pathlib import Path
P=Path(__file__).resolve().parents[1]/'packaging/client/upgrade.py';sys.path.insert(0,str(P.parent));spec=importlib.util.spec_from_file_location('upgrade_client',P);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
from scripts.sign_client_package import sign,public_record
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
class Tests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.signer=rsa.generate_private_key(public_exponent=65537,key_size=3072)
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.dest=self.root/'client';self.dest.mkdir();(self.dest/'.runtime').mkdir();(self.dest/'worker.py').write_text('old');(self.dest/'client.json').write_text('identity secret');self.pkg=self.root/'p.zip';self.sig=self.root/'sig.json';self.pub=self.root/'pub.json';self.key=self.root/'key';self.key.write_bytes(self.signer.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()));self.pub.write_text(json.dumps(public_record(self.signer)))
 def tearDown(self):self.tmp.cleanup()
 def archive(self,name='worker.py',digest=None):
  raw=b'new'
  with zipfile.ZipFile(self.pkg,'w') as z:z.writestr('company-agent-client/'+name,raw);z.writestr('company-agent-client/manifest.json',json.dumps({name:digest or hashlib.sha256(raw).hexdigest()}))
  self.sig.unlink(missing_ok=True);sign(self.pkg,self.key,self.sig)
 def apply(self):return mod.upgrade(self.pkg,self.dest,True,True,self.sig,self.pub)
 def test_preview_and_apply_preserve_identity_and_back_up_code(self):
  self.archive();r=mod.upgrade(self.pkg,self.dest);self.assertFalse(r['applied']);self.assertEqual((self.dest/'worker.py').read_text(),'old')
  with self.assertRaises(ValueError):mod.upgrade(self.pkg,self.dest,True)
  r=self.apply();self.assertEqual((Path(r['backup'])/'worker.py').read_text(),'old');self.assertEqual((self.dest/'worker.py').read_text(),'new');self.assertEqual((self.dest/'client.json').read_text(),'identity secret')
 def test_unsigned_apply_is_refused_before_changes(self):
  self.archive()
  with self.assertRaises(ValueError):mod.upgrade(self.pkg,self.dest,True,True)
  self.assertEqual((self.dest/'worker.py').read_text(),'old')
 def test_traversal_and_tampered_member_refused(self):
  for name,digest in [('../escape',None),('worker.py','f'*64)]:
   self.archive(name,digest)
   with self.assertRaises(ValueError):self.apply()
   self.assertEqual((self.dest/'worker.py').read_text(),'old')
 def test_running_work_or_unacked_result_blocks_upgrade(self):
  self.archive();p=self.dest/'.runtime/worker-win.sqlite3'
  with closing(sqlite3.connect(p)) as db:
   db.execute('CREATE TABLE work(state TEXT,acked INTEGER)');db.execute("INSERT INTO work VALUES('done',0)");db.commit()
  original=sqlite3.connect;connections=[]
  class Connection(sqlite3.Connection):
   closed=False
   def close(self):self.closed=True;super().close()
  def connect(*args,**kwargs):
   connection=original(*args,**kwargs,factory=Connection);connections.append(connection);return connection
  with patch.object(mod.sqlite3,'connect',side_effect=connect):
   with self.assertRaises(ValueError):self.apply()
  self.assertTrue(connections and all(c.closed for c in connections))
  self.assertEqual((self.dest/'worker.py').read_text(),'old')
 def test_relative_destination_and_inventory_are_supported(self):
  import os
  self.archive();previous=Path.cwd()
  try:
   os.chdir(self.root);mod.upgrade(Path('p.zip'),Path('client'),True,True,Path('sig.json'),Path('pub.json'))
  finally:os.chdir(previous)
  inventory=json.loads((self.dest/'installed-manifest.json').read_text())
  self.assertEqual(inventory['files'],{'worker.py':hashlib.sha256(b'new').hexdigest()})
  self.assertEqual(inventory['package_sha256'],hashlib.sha256(self.pkg.read_bytes()).hexdigest())
 def test_failed_inventory_commit_restores_code_and_old_inventory(self):
  self.archive();(self.dest/'installed-manifest.json').write_text('OLD INVENTORY');actual=Path.replace
  def replace(path,target):
   if Path(target).name=='installed-manifest.json':raise OSError('Simulated disk failure')
   return actual(path,target)
  with patch.object(Path,'replace',replace),self.assertRaises(OSError):self.apply()
  self.assertEqual((self.dest/'worker.py').read_text(),'old');self.assertEqual((self.dest/'installed-manifest.json').read_text(),'OLD INVENTORY')
  self.assertEqual((self.dest/'client.json').read_text(),'identity secret')
if __name__=='__main__':unittest.main()
