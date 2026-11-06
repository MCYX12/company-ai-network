"""Driver boundaries: gate status, execution codes, private paths and projection."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import ab_interleave, archive_evidence

ROOT=Path(ab_interleave.ROOT)


def row(side='A',sample='s',repeat=0,**extra):
    base={'attempt_id':'%s|%s|%d'%(side,sample,repeat),'side':side,'sample':sample,'repeat':repeat,'success':False}
    base.update(extra)
    return base


class InflightGateTests(unittest.TestCase):
    def test_missing_ledger_is_unknown_not_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            result=ab_interleave.inflight_status(folder,['t1'])
            self.assertEqual(result['status'],'unknown')
            self.assertEqual(result['reason'],'LEDGER_MISSING')

    def test_running_task_blocks_and_terminal_is_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            db=sqlite3.connect(Path(folder)/'tasks.sqlite3')
            db.execute('CREATE TABLE tasks(task_id TEXT,state TEXT)')
            db.execute("insert into tasks values('t1','running')")
            db.execute("insert into tasks values('t2','succeeded')")
            db.commit();db.close()
            self.assertEqual(ab_interleave.inflight_status(folder,['t1'])['status'],'inflight')
            self.assertEqual(ab_interleave.inflight_status(folder,['t2'])['status'],'clear')
            self.assertEqual(ab_interleave.inflight_status(folder,[])['status'],'clear')

    def test_observation_state_is_none_safe_and_chain_blocks(self):
        self.assertIsNone(ab_interleave.observation_state(None))
        with tempfile.TemporaryDirectory() as folder:
            db=sqlite3.connect(Path(folder)/'tasks.sqlite3')
            db.execute('CREATE TABLE tasks(task_id TEXT,state TEXT)')
            db.execute("insert into tasks values('child','running')")
            db.commit();db.close()
            failed=[row('A','s',0,state='failed',task_id='root')]
            observations=[{'attempt_id':'A|s|0','resolved':False,
                           'chain':[{'task_id':'root','state':'failed'},{'task_id':'child','state':'running'}],
                           'leaf':{'task_id':'child','state':'running'}}]
            self.assertEqual(ab_interleave.submission_gate(folder,failed,observations)['status'],'inflight')

    def test_no_observation_recovery_link_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            db=sqlite3.connect(Path(folder)/'tasks.sqlite3')
            db.execute('CREATE TABLE tasks(task_id TEXT,state TEXT)')
            db.execute("insert into tasks values('child','running')")
            db.commit();db.close()
            failed_with_child=[row('A','s',0,state='failed',task_id='root',retry_task_id='child')]
            self.assertEqual(ab_interleave.effective_state(failed_with_child[0],None),'unresolved')
            self.assertEqual(ab_interleave.submission_gate(folder,failed_with_child,[])['status'],'inflight')
            plain=[row('A','s',0,state='failed',task_id='root')]
            self.assertEqual(ab_interleave.submission_gate(folder,plain,[])['status'],'clear')

    def test_submission_gate_blocks_after_pending_first_side(self):
        with tempfile.TemporaryDirectory() as folder:
            db=sqlite3.connect(Path(folder)/'tasks.sqlite3')
            db.execute('CREATE TABLE tasks(task_id TEXT,state TEXT)')
            db.execute("insert into tasks values('t1','running')")
            db.commit();db.close()
            pending=[row('A','s',0,state=None,task_id='t1')]
            self.assertEqual(ab_interleave.submission_gate(folder,pending)['status'],'inflight')
            unknown=[row('A','s',0,state=None,task_id=None)]
            self.assertEqual(ab_interleave.submission_gate(folder,unknown)['status'],'unknown_submission')
            settled=[row('A','s',0,state='succeeded',task_id='t1')]
            self.assertEqual(ab_interleave.submission_gate(folder,settled)['status'],'clear')


class SettlementTests(unittest.TestCase):
    expected={'A|s|0','B|s|0'}

    def test_execution_success_requires_state_and_codes(self):
        rows=[row('A','s',0,state='succeeded',query_code='OK',result_code='OK'),
              row('B','s',0,state='succeeded',query_code='OK',result_code='FORBIDDEN')]
        summary=ab_interleave.summarize_attempts(rows,None,self.expected)
        self.assertEqual(summary['reconciled_success_rate'],0.5)
        self.assertFalse(summary['execution_pass'])

    def test_latest_observation_refusal_does_not_reuse_old_success(self):
        rows=[row('A','s',0,state='succeeded',query_code='OK',result_code='OK')]
        observations=[{'attempt_id':'A|s|0','resolved':False,
                       'leaf':{'state':None,'query_code':'FORBIDDEN','result_code':None}}]
        summary=ab_interleave.summarize_attempts(rows,observations,{'A|s|0'})
        self.assertEqual(summary['reconciled_success_rate'],0.0)
        self.assertFalse(summary['execution_pass'])
        self.assertEqual(summary['original_execution_state']['A|s|0'],'succeeded')

    def test_settled_leaf_with_codes_passes(self):
        rows=[row('A','s',0,state=None),row('B','s',0,state=None)]
        observations=[{'attempt_id':'A|s|0','resolved':True,'leaf':{'state':'succeeded','query_code':'OK','result_code':'OK'}},
                      {'attempt_id':'B|s|0','resolved':True,'leaf':{'state':'succeeded','query_code':'OK','result_code':'OK'}}]
        summary=ab_interleave.summarize_attempts(rows,observations,self.expected)
        self.assertEqual(summary['client_window_success_rate'],0.0)
        self.assertEqual(summary['reconciled_success_rate'],1.0)
        self.assertTrue(summary['results_settled'])
        self.assertTrue(summary['execution_pass'])


class PrivatePathTests(unittest.TestCase):
    def test_repo_path_refused_and_outside_is_0600(self):
        with self.assertRaises(SystemExit):
            ab_interleave.private_path(str(ROOT/'evidence'/'x.jsonl'))
        with tempfile.TemporaryDirectory() as folder:
            path=ab_interleave.private_path(str(Path(folder)/'sub'/'r.jsonl'))
            self.assertFalse(path.exists())
            ab_interleave.append_private(path,[{'a':1}])
            self.assertEqual(path.stat().st_mode & 0o777,0o600)


class PublicProjectionTests(unittest.TestCase):
    def test_projection_hides_questions_and_paths(self):
        report={'manifest':'m','seed':1,'orders':{},'meta':{'model':'m'},
                'summary':{'old':{},'new':{}},'compare':None,'expected_runs':1,'completed_runs':1,
                'attempts_executed':1,'client_window_success_rate':1.0,'reconciled_success_rate':1.0,
                'terminal_coverage':1.0,'results_settled':True,'execution_pass':True,
                'acceptance_scope':'execution-terminal only','missing':[],
                'sides':{'A':{'git':'abc','source_sha256':'d1','observed_digest':'x','digest_source':'observed'}}}
        projected=ab_interleave.public_report(report)
        self.assertNotIn('question',json.dumps(projected))
        with self.assertRaises(archive_evidence.PublicLeak):
            archive_evidence.assert_public({'samples':[{'question':'secret'}],'root':'/home/demo/x'})


if __name__=='__main__':
    unittest.main()
