"""Public evidence packages: task-scoped, and never contain question/answer bodies."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import archive_evidence

ROOT=Path(archive_evidence.ROOT)


class PublicGateTests(unittest.TestCase):
    def test_gate_rejects_bodies_credentials_and_absolute_paths(self):
        for bad in ({'tasks':[{'payload':{'question':'secret'}}]},
                    {'x':{'result':'an answer body'}},
                    {'y':{'token':'abc'}},
                    {'z':'/home/demo/private'}):
            with self.assertRaises(archive_evidence.PublicLeak, msg=str(bad)):
                archive_evidence.assert_public(bad)

    def test_gate_accepts_status_codes_timings_and_linkage(self):
        archive_evidence.assert_public({'tasks':[{'task_id':'t','state':'succeeded',
                                                  'result_code':'OK','timings':{'total_seconds':1},
                                                  'retry_of':'p'}]})


class PrivateBoundaryTests(unittest.TestCase):
    def test_repo_root_child_and_symlink_are_refused(self):
        with self.assertRaises(SystemExit):
            archive_evidence.private_target(str(ROOT))
        with self.assertRaises(SystemExit):
            archive_evidence.private_target(str(ROOT/'evidence'))
        with tempfile.TemporaryDirectory() as folder:
            link=Path(folder)/'link'
            link.symlink_to(ROOT,target_is_directory=True)
            with self.assertRaises(SystemExit):
                archive_evidence.private_target(str(link))

    def test_outside_is_0700_and_file_is_0600_at_creation(self):
        with tempfile.TemporaryDirectory() as folder:
            private=archive_evidence.private_target(str(Path(folder)/'private'))
            self.assertEqual(private.stat().st_mode & 0o777,0o700)
            target=archive_evidence.write_private(private,{'tasks':[]})
            self.assertEqual(target.stat().st_mode & 0o777,0o600)
            self.assertIn('full-bundle-',target.name)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.rt=Path(self.tmp.name)
        db=sqlite3.connect(self.rt/'tasks.sqlite3')
        db.execute('CREATE TABLE tasks(task_id TEXT,owner TEXT,target TEXT,operation TEXT,state TEXT,'
                   'created_at REAL,deadline REAL,attempts INTEGER,cancel_state TEXT,result TEXT)')
        db.execute('CREATE TABLE retries(root_task_id TEXT,retry_task_id TEXT,attempt INTEGER,'
                   'reason TEXT,created INTEGER)')
        db.execute("insert into tasks values('root','ag-b','ag-a','ask_peer','failed',1,2,1,null,"
                   "'{\"code\":\"WORKER_INTERRUPTED\",\"data\":{}}')")
        db.execute("insert into tasks values('child','ag-b','ag-a','ask_peer','succeeded',1,2,1,null,"
                   "'{\"code\":\"OK\",\"data\":{\"timings\":{\"total_seconds\":3}}}')")
        db.execute("insert into tasks values('other','ag-b','mac-x','ask_peer','succeeded',1,2,1,null,"
                   "'{\"code\":\"OK\",\"data\":{}}')")
        db.execute("insert into retries values('root','child',2,'WORKER_INTERRUPTED',1)")
        db.commit();db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_selection_expands_retry_chain_and_excludes_others(self):
        bundle=archive_evidence.build(self.rt,['root'])
        self.assertEqual(sorted(bundle['selection']['task_ids']),['child','root'])
        self.assertEqual({t['task_id'] for t in bundle['tasks']},{'root','child'})
        child=[t for t in bundle['tasks'] if t['task_id']=='child'][0]
        self.assertEqual(child['retry_of'],'root')
        self.assertEqual(child['timings'],{'total_seconds':3})
        self.assertEqual([r['retry_task_id'] for r in bundle['retries']],['child'])

    def test_public_bundle_passes_the_gate(self):
        bundle=archive_evidence.build(self.rt,['root'])
        archive_evidence.assert_public(bundle)
        serialised=json.dumps(bundle)
        self.assertNotIn('"payload"',serialised)
        self.assertNotIn('question',serialised)

    def test_missing_ledger_is_marked_not_empty(self):
        bundle=archive_evidence.build(self.rt/'absent',['root'])
        self.assertTrue(bundle['missing'])
        self.assertEqual(bundle['tasks'],[])

    def test_full_bundle_keeps_bodies_and_fails_the_public_gate(self):
        full=archive_evidence.full_bundle(self.rt,['root'])
        self.assertTrue(any('result' in row for row in full['tasks']))
        with self.assertRaises(archive_evidence.PublicLeak):
            archive_evidence.assert_public(full)


if __name__=='__main__':
    unittest.main()
