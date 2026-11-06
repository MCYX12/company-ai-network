"""The maintenance drain must be confirmable, defer by default and be cancelable."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import maintenance


class MaintenanceFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runtime=self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_state_rejects_linked_file_and_parent(self):
        root=Path(self.runtime);outside=root/'outside.json';outside.write_text('preserve')
        (root/'maintenance.json').symlink_to(outside)
        with self.assertRaises(ValueError):maintenance.begin(['ag-a'],runtime=root)
        self.assertEqual(outside.read_text(),'preserve')
        (root/'maintenance.json').unlink()
        (root/'linked').symlink_to(root,target_is_directory=True)
        with self.assertRaises(ValueError):maintenance.begin(['ag-a'],runtime=root/'linked')
        self.assertEqual(list(root.glob('.maintenance-*')),[])

    def test_begin_raises_marker_and_empty_targets_are_refused(self):
        with self.assertRaises(ValueError):
            maintenance.begin([],runtime=self.runtime)
        state=maintenance.begin(['ag-a','ag-a','ag-b'],max_wait=30,runtime=self.runtime,now=100)
        self.assertEqual(state['targets'],['ag-a','ag-b'])
        self.assertTrue((Path(self.runtime)/'maintenance').exists())
        self.assertTrue(maintenance.active(self.runtime))
        self.assertEqual(maintenance.read(self.runtime)['state'],'requested')

    def test_draining_needs_every_target_to_confirm(self):
        state=maintenance.begin(['ag-a','ag-b'],runtime=self.runtime,now=100)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=101,pending={})
        self.assertEqual(state['state'],'requested')
        state=maintenance.evaluate(state,['ag-a','ag-b'],runtime=self.runtime,now=102,pending={'ag-b':{'count':1}})
        self.assertEqual(state['state'],'draining')

    def test_settles_only_when_no_active_task_remains(self):
        state=maintenance.begin(['ag-a'],runtime=self.runtime,now=100)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=101,pending={'ag-a':{'count':2}})
        self.assertEqual(state['state'],'draining')
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=102,pending={})
        self.assertEqual(state['state'],'settled')
        self.assertFalse(state['truncated'] if 'truncated' in state else False)

    def test_timeout_defers_by_default_and_force_is_explicit(self):
        state=maintenance.begin(['ag-a'],max_wait=10,runtime=self.runtime,now=100)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=101,pending={'ag-a':{'count':1}})
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=200,pending={'ag-a':{'count':1}})
        self.assertEqual(state['state'],'deferred')
        maintenance.mark_forced(state,runtime=self.runtime,now=201)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=202,pending={'ag-a':{'count':1}})
        self.assertEqual(state['state'],'forced')
        self.assertTrue(state['forced'])

    def test_cancel_removes_the_marker_so_workers_resume(self):
        state=maintenance.begin(['ag-a'],runtime=self.runtime,now=100)
        maintenance.cancel(state,runtime=self.runtime,now=101)
        self.assertFalse(maintenance.active(self.runtime))
        self.assertFalse((Path(self.runtime)/'maintenance').exists())
        self.assertEqual(maintenance.status(self.runtime,now=102)['state'],'cancelled')

    def test_pending_tasks_reads_only_active_rows_for_the_targets(self):
        database=Path(self.runtime)/'tasks.sqlite3'
        db=sqlite3.connect(database)
        db.execute('CREATE TABLE tasks(task_id TEXT,target TEXT,state TEXT,deadline REAL)')
        db.executemany('INSERT INTO tasks VALUES(?,?,?,?)',[
            ('t1','ag-a','running',500),('t2','ag-a','succeeded',0),
            ('t3','ag-b','dispatched',600),('t4','ag-c','accepted',700)])
        db.commit();db.close()
        pending=maintenance.pending_tasks(['ag-a','ag-b'],path=database)
        self.assertEqual(pending['ag-a'],{'count':1,'deadline':500})
        self.assertEqual(pending['ag-b'],{'count':1,'deadline':600})
        self.assertNotIn('ag-c',pending)
        self.assertIsNone(maintenance.pending_tasks(['ag-a'],path=Path(self.runtime)/'missing.sqlite3'))


class WorkerPendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.runtime=self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_worker_in_flight_and_unacked_results_block_settlement(self):
        path=Path(self.runtime)/'worker-ag-a.sqlite3'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE work(task_id TEXT,state TEXT,acked INTEGER)')
        db.execute("INSERT INTO work VALUES('t1','running',0)")
        db.commit();db.close()
        self.assertEqual(maintenance.worker_pending(['ag-a'],self.runtime),
                         {'ag-a':{'running':1,'unacked':0}})
        state=maintenance.begin(['ag-a'],runtime=self.runtime,now=100)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=101,pending={})
        self.assertNotEqual(state['state'],'settled')
        # A persisted-but-unacknowledged result is still work owed to the coordinator.
        db=sqlite3.connect(path);db.execute("UPDATE work SET state='done',acked=0");db.commit();db.close()
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=102,pending={})
        self.assertNotEqual(state['state'],'settled')
        self.assertEqual(state['worker_pending']['ag-a'],{'running':0,'unacked':1})
        db=sqlite3.connect(path);db.execute('UPDATE work SET acked=1');db.commit();db.close()
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=103,pending={})
        self.assertEqual(state['state'],'settled')

    def test_missing_or_unreadable_local_ledger_blocks_settlement(self):
        state=maintenance.begin(['ag-a'],runtime=self.runtime,now=100)
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=101,pending={},local_targets=['ag-a'])
        self.assertNotEqual(state['state'],'settled')
        self.assertEqual(state['worker_unknown']['ag-a'],'LEDGER_MISSING')
        path=Path(self.runtime)/'worker-ag-a.sqlite3'
        path.write_bytes(b'not a sqlite database')
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=102,pending={},local_targets=['ag-a'])
        self.assertNotEqual(state['state'],'settled')
        self.assertEqual(state['worker_unknown']['ag-a'],'LEDGER_UNREADABLE')
        path.unlink()
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE work(task_id TEXT,state TEXT,acked INTEGER)')
        db.commit();db.close()
        state=maintenance.evaluate(state,['ag-a'],runtime=self.runtime,now=103,pending={},local_targets=['ag-a'])
        self.assertEqual(state['state'],'settled')

    def test_mark_deferred_persists_reason_and_blocking(self):
        state=maintenance.begin(['ag-a'],runtime=self.runtime,now=100)
        state=maintenance.mark_deferred(state,runtime=self.runtime,now=200,
                                        reason='MAINTENANCE_WAIT_EXCEEDED',
                                        blocking={'ag-a':{'count':1}},
                                        unknown={'ag-a':'LEDGER_UNREADABLE'})
        self.assertEqual(maintenance.read(self.runtime)['state'],'deferred')
        self.assertEqual(state['deferred_reason'],'MAINTENANCE_WAIT_EXCEEDED')
        self.assertEqual(state['blocking'],{'ag-a':{'count':1}})
        self.assertEqual(state['worker_unknown'],{'ag-a':'LEDGER_UNREADABLE'})

    def test_stop_gate_requires_every_local_worker(self):
        from scripts.maintenance import targets_cover
        self.assertTrue(targets_cover(['ag-a'],['ag-a','ubuntu-harness-01']))
        self.assertTrue(targets_cover([],['ag-a']))
        self.assertFalse(targets_cover(['ag-a','ubuntu-harness-01'],['ag-a']))

    def test_connection_failure_is_deferred_and_never_stops_service(self):
        from scripts import maintenance as cli
        from types import SimpleNamespace
        from unittest.mock import patch
        maintenance.begin(['ag-a'],runtime=self.runtime)
        args=SimpleNamespace(runtime=self.runtime,identity='ag-a',endpoint='tls://127.0.0.1:1',
                             stop=True,allow_partial_targets=False)
        actual_read=maintenance.read
        with patch.object(cli.maintenance,'read',side_effect=lambda runtime=None:actual_read(runtime or self.runtime)), \
             patch.object(cli,'local_workers',return_value=({'ag-a'},False)), \
             patch.object(cli,'draining_targets',side_effect=TimeoutError()),patch.object(cli,'stop_unit') as stop:
            self.assertEqual(cli.cmd_run(args),3);stop.assert_not_called()
        state=maintenance.read(self.runtime);self.assertEqual(state['state'],'deferred')
        self.assertEqual(state['deferred_reason'],'MAINTENANCE_CONNECTION_FAILED')


if __name__=='__main__':
    unittest.main()
