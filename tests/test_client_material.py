import asyncio,importlib.util,json,os,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
import sys
from app.client_material import collect,summarize,read_bytes,safe_name
from tests.helpers.client import load_client_worker

class MaterialTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_question_selects_late_section_and_reports_coverage(self):
  (self.root/'README.md').write_text('# 简介\n'+('背景资料\n'*900)+'\n# 越权拒绝码\n跨项目拒绝代码为 FORBIDDEN。',encoding='utf-8')
  text,c=collect(self.root,['README.md'],'越权拒绝码是什么',600)
  self.assertIn('FORBIDDEN',text);self.assertLessEqual(len(text),600);self.assertGreater(c['omitted_total'],0);self.assertTrue(c['truncated']);self.assertTrue(c['selected'][0]['sha256']);self.assertGreater(c['selected'][0]['start_line'],900)
 def test_safe_off_do_not_read_contents(self):
  (self.root/'README.md').write_text('SECRET BODY')
  with patch('app.client_material.read_bytes',side_effect=AssertionError('read')):
   text,c=collect(self.root,['README.md'],'x',mode='safe');self.assertNotIn('SECRET BODY',text);self.assertEqual(c['read_bytes'],0)
   self.assertIsNone(collect(self.root,['README.md'],'x',mode='off')[0])
 def test_link_and_mutation_are_rejected(self):
  outside=self.root/'outside';outside.write_text('secret');os.link(outside,self.root/'README.md')
  text,c=collect(self.root,['README.md'],'secret');self.assertNotIn('secret',text);self.assertTrue(c['skipped'])
  with self.assertRaises(ValueError):read_bytes(self.root,'../outside')
 def test_oversize_is_explicit(self):
  (self.root/'README.md').write_bytes(b'x'*262145)
  text,c=collect(self.root,['README.md'],'x');self.assertEqual(c['skipped'][0]['reason'],'BYTE_LIMIT');self.assertEqual(c['read_bytes'],0)
 def test_missing_or_empty_material_is_not_complete_coverage(self):
  for present in (False,True):
   if present:(self.root/'README.md').write_text('')
   text,c=collect(self.root,['README.md'],'x');self.assertFalse(c['complete']);self.assertEqual(c['selected'],[])
 def test_revocation_after_read_propagates_without_releasing_excerpt(self):
  (self.root/'README.md').write_text('PRIVATE')
  calls=[]
  def check():
   calls.append(1)
   if len(calls)==2:raise PermissionError('Revoked')
  with self.assertRaises(PermissionError):collect(self.root,['README.md'],'x',check=check)
 def test_windows_aliases_controls_and_device_names_are_rejected(self):
  for name in ('README.md\x00','README.md.','README.md ','NUL.txt','nested/COM1.log','bad?.md'):
   with self.subTest(name=name),self.assertRaises(ValueError):safe_name(name)
  self.assertEqual(safe_name('资料/测试.md'),'资料/测试.md')
 def test_truncated_excerpt_has_its_own_hash_and_explicit_coverage(self):
  import hashlib
  (self.root/'README.md').write_text('x'*600)
  text,c=collect(self.root,['README.md'],'x',budget=150);row=c['selected'][0]
  self.assertFalse(row['excerpt_complete']);self.assertFalse(c['complete']);self.assertNotEqual(row['sha256'],row['excerpt_sha256'])
  self.assertEqual(row['excerpt_sha256'],hashlib.sha256(text.split('\n',1)[1].encode()).hexdigest())
 def test_unknown_summary_citations_are_rejected(self):
  c={'selected':[{'id':'known'}]}
  for raw in ['{"facts":[{"summary":"已上线","evidence_ids":["invented"]}]}','{"facts":[]}','not json']:
   with self.assertRaises(ValueError):summarize('input',c,'local',time.time()+60,lambda *a,**k:{'response':raw})
 def test_valid_summary_has_provenance_and_no_raw_upload(self):
  r=summarize('text',{'selected':[{'id':'c1'}]},'local',time.time()+60,lambda *a,**k:{'response':'{"facts":[{"summary":"文档说尚待确认","evidence_ids":["c1"]}]}'})
  self.assertFalse(r['raw_uploaded']);self.assertEqual(r['basis'],'documented_not_independently_verified')
 def test_local_failure_never_invokes_cloud_answer(self):
  w=load_client_worker();(self.root/'README.md').write_text('APPROVED_SECRET')
  envelope={'operation':'ask_peer','payload':{'question':'项目进度','caller':'caller'},'deadline':time.time()+60,'target':'t'}
  grant={'id':'g','root':str(self.root),'expires':time.time()+60}
  with patch.dict(sys.modules,{'ollama_adapter':SimpleNamespace(generate=Mock(side_effect=ValueError('failed')))}),patch.object(w,'run_command') as cloud:
   r=asyncio.run(w.make_executor({'notes_mode':'local','local_summary_model':'local'})(envelope,[grant],[]));self.assertEqual(r['code'],'LOCAL_SUMMARY_UNAVAILABLE');cloud.assert_not_called()
