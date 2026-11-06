import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from app.task_store import TaskStore
from scripts import retention

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'
DAY = 86400.0


class RetentionTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.work = Path(self.tmp.name)
        self.runtime = self.work / 'runtime'
        self.runtime.mkdir()
        now = time.time()
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        self.old_terminal = store.submit('ag-a', 'ag-b', 'old-term', 'ask_peer', {'q': 'x'}, 300)['task_id']
        store.finish(self.old_terminal, {'code': 'OK', 'data': {}})
        self.recent_terminal = store.submit('ag-a', 'ag-b', 'recent-term', 'ask_peer', {'q': 'x'}, 300)['task_id']
        store.finish(self.recent_terminal, {'code': 'OK', 'data': {}})
        self.active = store.submit('ag-a', 'ag-b', 'active', 'ask_peer', {'q': 'x'}, 300)['task_id']
        db = sqlite3.connect(self.runtime / 'tasks.sqlite3')
        db.execute('UPDATE tasks SET created_at=? WHERE task_id=?', (now - 100 * DAY, self.old_terminal))
        db.execute('UPDATE tasks SET created_at=? WHERE task_id=?', (now - 100 * DAY, self.active))
        db.execute('INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,?,?)', (self.old_terminal, 'accepted', now - 400 * DAY))
        db.execute('INSERT INTO task_audit(task_id,event,timestamp) VALUES(?,?,?)', (self.recent_terminal, 'accepted', now - DAY))
        db.execute('INSERT INTO task_archive(task_id,state,archived_at) VALUES(?,?,?)', ('old-arch', 'succeeded', now - 400 * DAY))
        db.commit(); db.close()
        worker = sqlite3.connect(self.runtime / 'worker-mac-01.sqlite3')
        worker.execute('CREATE TABLE work(task_id TEXT PRIMARY KEY,fingerprint TEXT,state TEXT,result TEXT,acked INT,updated_at REAL)')
        worker.execute('INSERT INTO work VALUES(?,?,?,?,?,?)', ('old-work', 'f', 'done', None, 1, now - 100 * DAY))
        worker.execute('INSERT INTO work VALUES(?,?,?,?,?,?)', ('running-work', 'f', 'running', None, 0, now - 100 * DAY))
        worker.commit(); worker.close()
        history = sqlite3.connect(self.runtime / 'history-access.sqlite3')
        history.execute('CREATE TABLE requests(id TEXT PRIMARY KEY,caller TEXT,target TEXT,state TEXT,created REAL,expires REAL,actor TEXT,reason TEXT,scope TEXT)')
        history.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,request_id TEXT,event TEXT,time REAL,actor TEXT,reason TEXT)')
        history.execute('INSERT INTO events(request_id,event,time) VALUES(?,?,?)', ('r', 'grant', now - 400 * DAY))
        history.execute('INSERT INTO requests(id,state,created,expires) VALUES(?,?,?,?)', ('old-req', 'expired', now - 500 * DAY, now - 400 * DAY))
        history.commit(); history.close()
        (self.work / 'backups/old').mkdir(parents=True)
        (self.work / 'backups/new').mkdir()
        os.utime(self.work / 'backups/old', (now - 100 * DAY, now - 100 * DAY))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_counts_only_expired(self):
        report = retention.plan(self.runtime, backups_dir=self.work / 'backups')
        self.assertEqual(report['tasks'], 1)
        self.assertEqual(report['task_audit'], 1)
        self.assertEqual(report['task_archive'], 1)
        self.assertEqual(report['history_events'], 1)
        self.assertEqual(report['history_requests'], 1)
        self.assertEqual(report['worker_done'], 1)
        self.assertEqual(report['backups'], [str(self.work / 'backups/old')])

    def test_apply_requires_confirmation(self):
        with self.assertRaises(ValueError):
            retention.apply(self.runtime)

    def test_apply_deletes_only_expired_and_keeps_in_flight(self):
        report = retention.apply(self.runtime, confirm=True, backups_dir=self.work / 'backups')
        self.assertEqual(report['deleted']['tasks'], 1)
        db = sqlite3.connect(self.runtime / 'tasks.sqlite3')
        ids = {row[0] for row in db.execute('SELECT task_id FROM tasks')}
        db.close()
        self.assertIn(self.recent_terminal, ids)
        self.assertIn(self.active, ids)
        self.assertNotIn(self.old_terminal, ids)
        worker = sqlite3.connect(self.runtime / 'worker-mac-01.sqlite3')
        rows = {row[0] for row in worker.execute('SELECT task_id FROM work')}
        worker.close()
        self.assertIn('running-work', rows)
        self.assertNotIn('old-work', rows)
        self.assertFalse((self.work / 'backups/old').exists())
        self.assertTrue((self.work / 'backups/new').exists())


if __name__ == '__main__':
    unittest.main()
