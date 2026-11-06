import json
from pathlib import Path
import tempfile
import unittest
import uuid
from app.task_store import TaskStore,WorkerStore
from scripts.runtime_backup import backup,restore,verify

class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.runtime=self.root/'runtime';self.runtime.mkdir()
        self.store=TaskStore(self.runtime/'tasks.sqlite3')
        self.done=self.store.submit('ag-a','ag-b','done','get_status',{},60);self.store.finish(self.done['task_id'],{'code':'OK','data':{'answer':'preserved'}})
        self.cancelled=self.store.submit('ag-a','ag-b','cancel','get_status',{},60);self.store.cancel(self.cancelled['task_id'],'ag-a')
        self.pending=self.store.submit('ag-a','ag-b','pending','get_status',{},60)
        (self.runtime/'registry.json').write_text('{"schema_version":1,"agents":[]}')
        (self.runtime/'certs').mkdir();(self.runtime/'certs/ca.key').write_text('DO NOT COPY')
        self.snapshot=self.root/'snapshot';self.dest=self.root/'restored'
    def tearDown(self):self.store.db.close();self.tmp.cleanup()
    def test_project_retirement_and_site_survive_restore(self):
        tombstone={'format':1,'retired':['demo-project']}
        (self.runtime/'project-retirements.json').write_text(json.dumps(tombstone))
        (self.runtime/'site.json').write_text('{"org":{"id":"enterprise"}}')
        backup(self.runtime,self.snapshot);restore(self.snapshot,self.dest)
        self.assertEqual(json.loads((self.dest/'project-retirements.json').read_text()),tombstone)
        self.assertTrue((self.dest/'site.json').is_file())
    def test_open_wal_db_and_terminal_results_restore(self):
        manifest=backup(self.runtime,self.snapshot)
        self.assertFalse((self.snapshot/'certs').exists());self.assertFalse((self.snapshot/'tasks.sqlite3-wal').exists())
        self.assertEqual(verify(self.snapshot),manifest)
        report=restore(self.snapshot,self.dest);self.assertEqual(report['pending_tasks_held'],1)
        restored=TaskStore(self.dest/'tasks.sqlite3')
        try:
            self.assertEqual(restored.public(restored.get(self.done['task_id']))['result']['data']['answer'],'preserved')
            self.assertEqual(restored.get(self.cancelled['task_id'])['state'],'cancelled')
            held=restored.submit('ag-a','ag-b','pending','get_status',{},60)
            self.assertEqual(held['task_id'],self.pending['task_id']);self.assertEqual(held['state'],'failed')
            self.assertEqual(restored.due(),[])
        finally:restored.db.close()
    def test_live_destination_cannot_be_overwritten(self):
        backup(self.runtime,self.snapshot)
        with self.assertRaises(ValueError):restore(self.snapshot,self.runtime)
        self.assertEqual(self.store.get(self.pending['task_id'])['state'],'accepted')
    def test_tampering_rejected_before_destination_created(self):
        backup(self.runtime,self.snapshot);(self.snapshot/'registry.json').write_text('{}')
        with self.assertRaises(ValueError):restore(self.snapshot,self.dest)
        self.assertFalse(self.dest.exists())
    def test_manifest_path_traversal_rejected(self):
        backup(self.runtime,self.snapshot);p=self.snapshot/'manifest.json';data=json.loads(p.read_text());data['files'][0]['name']='../private';p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):restore(self.snapshot,self.dest)
        self.assertFalse(self.dest.exists())
    def test_worker_outbox_and_uncertain_execution_retained(self):
        worker=WorkerStore(self.runtime/'worker-ag-b.sqlite3')
        envelope={'task_id':str(uuid.uuid4()),'target':'ag-b','operation':'get_status','payload':{},'deadline':9999999999}
        worker.claim(envelope)
        done={**envelope,'task_id':str(uuid.uuid4())};worker.claim(done);worker.finish(done['task_id'],{'code':'OK','data':{'result':'saved'}})
        try:backup(self.runtime,self.snapshot)
        finally:worker.db.close()
        report=restore(self.snapshot,self.dest);self.assertEqual(report['interrupted_workers'],1)
        restored=WorkerStore(self.dest/'worker-ag-b.sqlite3')
        try:
            self.assertFalse(restored.claim(done));self.assertFalse(restored.claim(envelope))
            self.assertEqual(len(restored.outbox()),2)
            self.assertEqual(dict(restored.outbox())[envelope['task_id']]['code'],'WORKER_INTERRUPTED')
        finally:restored.db.close()
    def test_account_and_authorization_ledgers_are_backed_up(self):
        import sqlite3
        accounts=sqlite3.connect(self.runtime/'accounts.sqlite3')
        accounts.executescript('''CREATE TABLE admins(name TEXT PRIMARY KEY,salt TEXT,hash TEXT,enabled INTEGER);
CREATE TABLE sessions(token TEXT PRIMARY KEY,admin TEXT,expires REAL);
CREATE TABLE invites(token TEXT PRIMARY KEY,role TEXT,expires REAL,projects TEXT,used INTEGER DEFAULT 0);
CREATE TABLE members(id TEXT PRIMARY KEY,name TEXT,device TEXT,projects TEXT,state TEXT);
CREATE TABLE account_events(time REAL,actor TEXT,event TEXT,subject TEXT);
CREATE TABLE login_attempts(time REAL);''')
        accounts.execute("INSERT INTO admins VALUES('admin','s','h',1)")
        accounts.execute("INSERT INTO sessions VALUES('tok','admin',9e9)")
        accounts.execute("INSERT INTO members VALUES('m1','Alice','dev','[\"demo-project\"]','active')")
        accounts.commit();accounts.close()
        access=sqlite3.connect(self.runtime/'conversation-access.sqlite3')
        access.executescript('''CREATE TABLE approvals (id TEXT PRIMARY KEY, caller TEXT, target TEXT, projects TEXT, state TEXT, created REAL, expires REAL, reason TEXT);
CREATE TABLE access_events (id INTEGER PRIMARY KEY, approval TEXT, event TEXT, timestamp REAL, reason TEXT);''')
        access.execute("INSERT INTO approvals VALUES('a1','ag-a','ag-b','[\"demo-project\"]','approved',1.0,9e9,'x')")
        access.commit();access.close()
        manifest=backup(self.runtime,self.snapshot)
        names={entry['name'] for entry in manifest['files']}
        self.assertIn('accounts.sqlite3',names);self.assertIn('conversation-access.sqlite3',names)
        report=restore(self.snapshot,self.dest)
        self.assertEqual(report['sessions_invalidated'],1)
        self.assertEqual(report['ledgers']['members'],1)
        self.assertEqual(report['ledgers']['conversation_approvals'],1)
        restored=sqlite3.connect(self.dest/'accounts.sqlite3')
        try:
            self.assertEqual(restored.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(restored.execute('SELECT count(*) FROM members').fetchone()[0],1)
        finally:restored.close()

    def test_unlisted_database_is_not_silently_included(self):
        (self.runtime/'evil.sqlite3').write_bytes(b'not backed up')
        manifest=backup(self.runtime,self.snapshot)
        self.assertNotIn('evil.sqlite3',{entry['name'] for entry in manifest['files']})

    def test_symlink_and_repeated_backup_rejected(self):
        backup(self.runtime,self.snapshot)
        with self.assertRaises(ValueError):backup(self.runtime,self.snapshot)
        (self.runtime/'worker-fake.sqlite3').symlink_to(self.runtime/'tasks.sqlite3')
        with self.assertRaises(ValueError):backup(self.runtime,self.root/'second')
if __name__=='__main__':unittest.main()
