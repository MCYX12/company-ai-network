"""Approved-directory notes in the prompt: local facts when cached, metadata otherwise.

The standing rule is that the approved 说明/待办 原文 never reaches a networked model;
only a local summarizer's facts and a metadata inventory may be embedded.
"""
import json,os,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

from app import local_progress
from app.directory_context import CURRENT

GRANT={'id':'g1','caller':'ag-a','target':'ag-a','root':'','profile':'project_notes_v1',
       'state':'approved','expires':4102444800.0}
ROW={'agent_id':'ag-a','device_id':'ubuntu-demo','employee_id':'e','tool':'dsh-harness',
     'projects':['demo-project'],'enabled':True}
SECRET='机密句子不应出现在提示里'


def make_repo(path):
    path.mkdir(parents=True,exist_ok=True)
    (path/'README.md').write_text('# 标题\n'+SECRET+'\n')
    (path/'TODO.md').write_text('- [ ] 待办 A\n')
    for argv in (['init','-q'],['config','user.email','t@t'],['config','user.name','t'],['add','-A'],['commit','-qm','init']):
        subprocess.run(['/usr/bin/git',*argv],cwd=path,check=True,capture_output=True)
    (path/'untracked.txt').write_text('x')


class NotesDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)/'Proj'
        make_repo(self.root)
        self.grant=dict(GRANT,root=str(self.root))
        CURRENT.set([self.grant])

    def tearDown(self):
        CURRENT.set(None)
        self.tmp.cleanup()

    def _digest(self,mode='safe'):
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE',mode):
            return local_progress.notes_digest('ag-a','ag-a',str(self.root),900)

    def test_safe_mode_lists_documents_and_git_but_no_raw_text(self):
        block=self._digest('safe')
        self.assertIn('已授权目录',block)
        self.assertIn('README.md',block)
        self.assertIn('只读 git 状态',block)
        self.assertIn('untracked=1',block)
        self.assertNotIn(SECRET,block)

    def test_cached_local_report_facts_are_included_instead_of_the_inventory(self):
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)):
            path=local_progress.cache_path('ag-a','ag-a',str(self.root))
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps({'project':'Proj','project_path':str(self.root),'caller':'ag-a',
                'target':'ag-a','expires':time.time()+300,'notes_allowed':True,'generated_at':time.time(),
                'local_model':'qwen3.5:9b','raw_uploaded':False,
                'facts':[{'summary':'已完成 A 模块','basis':'documented','evidence_ids':['E1']}],
                'git':{'available':True,'changed_entries':2,'untracked_entries':0},
                'limitations':['未运行代码或测试'],'evidence':[]},ensure_ascii=False))
            os.chmod(path,0o600)   # same restrictive mode the platform writes reports with
            block=local_progress.notes_digest('ag-a','ag-a',str(self.root),900)
        self.assertIn('已完成 A 模块',block)
        self.assertIn('changed=2',block)
        self.assertIn('未运行代码或测试',block)
        self.assertIn('摘要资料覆盖',block)
        self.assertNotIn(SECRET,block)

    def test_raw_mode_is_the_owner_opt_in(self):
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),patch.object(local_progress,'NOTES_MODE','raw'):
            block=local_progress.notes_digest('ag-a','ag-a',str(self.root),4000)
        self.assertIn(SECRET,block)

    def test_off_mode_and_unauthorized_root_produce_nothing(self):
        self.assertIsNone(self._digest('off'))
        CURRENT.set([])
        self.assertIsNone(self._digest('safe'))

    def test_prompt_includes_notes_and_stays_within_the_hard_limit(self):
        from app.peer_question import build_prompt
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','safe'):
            prompt,sources=build_prompt(ROW,{'question':'这个项目进展如何？','projects':['demo-project'],'caller':'ag-a'})
        self.assertIn('已授权目录',prompt)
        self.assertIn('project_notes',{s.get('kind') for s in sources if isinstance(s,dict)})
        self.assertLessEqual(len(prompt),4000)
        self.assertNotIn(SECRET,prompt)

    def test_notes_context_ignores_grants_for_other_callers(self):
        from app.peer_question import notes_context
        CURRENT.set([dict(self.grant,caller='ag-b')])
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)):
            block,roots=notes_context(ROW,{'question':'q','projects':['demo-project'],'caller':'ag-a'})
        self.assertEqual(block,'')
        self.assertEqual(roots,[])


    def test_harness_prompt_sees_the_dispatch_grants(self):
        """The one-shot harness path used to build its prompt with no grants at all."""
        from app import harness_adapter
        from app.directory_context import scope
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','safe'):
            payload={'question':'进展如何？','projects':['demo-project'],'caller':'ag-a'}
            grants=[{'id':'g1','caller':'ag-a','target':'ag-a','root':str(self.root),
                     'profile':'project_notes_v1','state':'approved','expires':4102444800.0}]
            with scope(ROW,payload,grants,[]):
                prompt,roots=harness_adapter._prompt(ROW,payload,{'prompt_prefix':'规则'})
        self.assertEqual(roots,[str(self.root)])
        self.assertIn('已授权目录',prompt)
        self.assertIn('README.md',prompt)
        # Platform contract first, then the device owner's rule, then the material.
        self.assertTrue(prompt.startswith(harness_adapter.PLATFORM_RULES))
        self.assertIn('规则',prompt)
        self.assertNotIn(SECRET,prompt)

    def test_harness_path_reports_digest_coverage(self):
        from app import harness_adapter
        from app.directory_context import scope
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','raw'):
            payload={'question':'项目进展如何？','projects':['demo-project'],'caller':'ag-a'}
            grants=[dict(self.grant,root=str(self.root))]
            coverage={}
            with scope(ROW,payload,grants,[]):
                _prompt,roots=harness_adapter._prompt(ROW,payload,None,coverage=coverage)
        self.assertEqual(roots,[str(self.root)])
        self.assertIn('truncated',coverage)
        self.assertGreaterEqual(coverage.get('digested_total',0),1)
        self.assertGreaterEqual(coverage.get('present_total'),1)

    def test_grant_scope_validates_and_restores(self):
        from app.directory_context import CURRENT,scope
        CURRENT.set(None)
        payload={'question':'q','projects':['demo-project'],'caller':'ag-a'}
        bad=[{'id':'g','caller':'ag-b','target':'ag-a','root':'/Users/mac/proj',
              'profile':'project_notes_v1','state':'approved','expires':9e9}]
        with self.assertRaises(ValueError):
            with scope(ROW,payload,bad,[]):pass
        self.assertIsNone(CURRENT.get())
        good=[dict(bad[0],caller='ag-a')]
        with scope(ROW,payload,good,[]):
            self.assertEqual(CURRENT.get(),good)
        self.assertIsNone(CURRENT.get())


class NotesRootSelectionTests(unittest.TestCase):
    """Which granted roots reach the prompt, and which ones we report as used."""

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.base=Path(self.tmp.name)

    def tearDown(self):
        CURRENT.set(None)
        self.tmp.cleanup()

    def _project(self,name):
        path=self.base/name
        path.mkdir(parents=True,exist_ok=True)
        (path/'README.md').write_text('# %s\n'%name)
        return path

    def _grants(self,roots):
        return [{'id':'g%d'%i,'caller':'ag-a','target':'ag-a','root':str(root),
                 'profile':'project_notes_v1','state':'approved','expires':4102444800.0}
                for i,root in enumerate(roots)]

    def _context(self,question,roots,budget=1200):
        from app.peer_question import notes_context
        CURRENT.set(self._grants(roots))
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','safe'):
            return notes_context(ROW,{'question':question,'projects':['demo-project'],'caller':'ag-a'},
                                 budget=budget)

    def test_root_named_in_the_question_precedes_alphabetical_order(self):
        aaa=self._project('AAA'); robotac=self._project('ROBOTAC')
        block,roots=self._context('ROBOTAC 项目最近进展如何？',[aaa,robotac])
        self.assertEqual(roots[0],str(robotac))
        self.assertEqual(set(roots),{str(aaa),str(robotac)})
        self.assertIn(str(robotac),block)

    def test_shallower_root_precedes_its_own_subdirectory(self):
        parent=self._project('Proj'); child=parent/'sub'; child.mkdir()
        block,roots=self._context('最近在忙什么？',[child,parent])
        self.assertEqual(roots,[str(parent),str(child)])

    def test_selection_is_capped_and_budget_aware(self):
        roots=[self._project('R%d'%i) for i in range(8)]
        _block,got=self._context('q',roots,budget=1200)
        self.assertEqual(len(got),5)                      # MAX_NOTES_DIGESTS
        _block,small=self._context('q',roots,budget=240)  # 240//120 => 2 roots
        self.assertEqual(len(small),2)
        _block,none=self._context('q',roots,budget=60)    # below the digest floor
        self.assertEqual(none,[])

    def test_reported_roots_are_exactly_the_digested_ones(self):
        root=self._project('Only')
        from app.peer_question import notes_context
        CURRENT.set(self._grants([root]))
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','off'):
            block,roots=notes_context(ROW,{'question':'q','projects':['demo-project'],'caller':'ag-a'})
        self.assertEqual(block,'')
        self.assertEqual(roots,[])   # nothing digested -> nothing claimed as used

    def test_harness_path_reports_the_same_roots_it_embedded(self):
        from app import harness_adapter
        from app.directory_context import scope
        listed=self._project('AAA'); named=self._project('ROBOTAC')
        payload={'question':'ROBOTAC 的计划是什么？','projects':['demo-project'],'caller':'ag-a'}
        with patch('app.local_progress.RUNTIME',Path(self.tmp.name)),\
             patch.object(local_progress,'NOTES_MODE','safe'):
            with scope(ROW,payload,self._grants([listed,named]),[]):
                prompt,roots=harness_adapter._prompt(ROW,payload,None)
        self.assertEqual(roots[0],str(named))
        self.assertIn(str(named),prompt)
        self.assertLessEqual(len(prompt),4000)


if __name__=='__main__':
    unittest.main()
