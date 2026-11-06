import os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from app.file_access import FileAccess,read_safe
class FileTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.f=self.root/'README.md';self.f.write_text('项目进展：完成界面，等待联调。');self.s=FileAccess(self.root/'db')
 def tearDown(self):self.tmp.cleanup()
 def grant(self,**kw):return self.s.create('a','b','p',str(self.f),**kw)
 def test_read_binding_cloud_and_revoke(self):
  g=self.grant();self.assertIn('项目进展',self.s.read(g,'a','b','p')[0]['text'])
  for args in [('x','b','p'),('a','x','p'),('a','b','x')]:
   with self.assertRaises(ValueError):self.s.read(g,*args)
  with self.assertRaises(ValueError):self.s.read(g,'a','b','p',for_cloud=True)
  self.s.revoke(g)
  with self.assertRaises(ValueError):self.s.read(g,'a','b','p')
 def test_expiry_and_file_change(self):
  g=self.grant(seconds=1)
  with patch('app.file_access.time.time',return_value=time.time()+2):
   with self.assertRaises(ValueError):self.s.read(g,'a','b','p')
  self.f.write_text('新的内容')
  with self.assertRaises(ValueError):self.s.read(g,'a','b','p')
 def test_links_hidden_secret_size_and_traversal(self):
  link=self.root/'link.md';link.symlink_to(self.f)
  with self.assertRaises(OSError):read_safe(link)
  link.unlink();os.link(self.f,link)
  with self.assertRaises(ValueError):read_safe(self.f)
  link.unlink()
  for value in ['password=secret-value','-----BEGIN RSA '+'PRIVATE KEY-----','x'*262145]:
   self.f.write_text(value)
   with self.assertRaises(ValueError):read_safe(self.f)
  with self.assertRaises(ValueError):read_safe(self.root/'..'/'README.md')
 def test_directory_snapshot_excludes_new_and_depth(self):
  sub=self.root/'sub';sub.mkdir();(sub/'a.md').write_text('child')
  g=self.s.create('a','b','p',str(self.root),kind='directory',depth=0)
  (self.root/'new.md').write_text('new')
  self.assertEqual([x['name'] for x in self.s.read(g,'a','b','p')],['README.md'])
 def test_revoke_during_read_returns_no_content(self):
  g=self.grant();original=read_safe
  def revoke(path):
   result=original(path);self.s.revoke(g);return result
  with patch('app.file_access.read_safe',side_effect=revoke):
   with self.assertRaises(ValueError):self.s.read(g,'a','b','p')
 def test_budget(self):
  g=self.grant()
  with self.s.db() as db:db.execute('UPDATE scopes SET calls=100 WHERE id=?',(g,))
  with self.assertRaises(ValueError):self.s.read(g,'a','b','p')
 def test_model_context_requires_explicit_cloud_permission(self):
  from app.peer_question import build_prompt
  row={'agent_id':'b','projects':['p']};payload={'caller':'a','projects':['p'],'question':'项目如何？'}
  g=self.grant()
  with patch('app.file_access.FileAccess',return_value=self.s),patch('app.peer_question.read_progress',return_value=('DATA_UNAVAILABLE',None)):
   prompt,sources=build_prompt(row,payload);self.assertNotIn('完成界面',prompt)
   self.s.revoke(g);self.grant(cloud=True)
   prompt,sources=build_prompt(row,payload);self.assertIn('完成界面',prompt);self.assertTrue(any(s.get('scope_id') for s in sources))
