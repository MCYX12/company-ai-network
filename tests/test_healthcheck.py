from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from app.healthcheck import expiry_status,local_checks

class HealthTests(unittest.TestCase):
    def test_expiry_boundary(self):
        self.assertEqual(expiry_status(0),'fail');self.assertEqual(expiry_status(-1),'fail')
        self.assertEqual(expiry_status(3600),'warn');self.assertEqual(expiry_status(72*3600),'ok')
    def test_empty_runtime_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            checks=local_checks(folder)
            self.assertEqual(next(c for c in checks if c['check']=='certificates')['status'],'fail')
            self.assertEqual(next(c for c in checks if c['check']=='databases')['status'],'fail')
    def test_corrupt_database_and_exposed_key_detected_without_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'certs').mkdir();key=root/'certs/agent.key';key.write_text('secret-must-not-appear');key.chmod(0o644)
            (root/'tasks.sqlite3').write_bytes(b'corrupt data')
            checks=local_checks(root)
            self.assertEqual(next(c for c in checks if c['check']=='database:tasks.sqlite3')['status'],'fail')
            self.assertEqual(next(c for c in checks if c['check']=='private_key_permissions')['status'],'fail')
            self.assertNotIn('secret-must-not-appear',str(checks))
    def test_dead_letter_counts_lifetime_and_recent_window(self):
        import sqlite3
        import time
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'certs').mkdir();now=time.time()
            db=sqlite3.connect(root/'tasks.sqlite3')
            db.execute("CREATE TABLE tasks(task_id TEXT PRIMARY KEY, state TEXT, result TEXT, created_at REAL, target TEXT)")
            db.executemany("INSERT INTO tasks VALUES(?,?,?,?,?)",[
                ('recent','failed','DELIVERY_FAILED: timeout',now-60,'mac-codex-01'),
                ('recent2','failed','DELIVERY_FAILED: timeout',now-120,'mac-codex-01'),
                ('recent3','failed','DELIVERY_FAILED: timeout',now-180,'mac-harness-01'),
                ('old','failed','DELIVERY_FAILED: gone',now-3*86400,'mac-codex-01'),
                ('other','failed','EXECUTION_FAILED',now-60,'mac-codex-01')])
            db.commit();db.close()
            detail=next(c for c in local_checks(root) if c['check']=='task_ledger')['detail']
            self.assertEqual(detail['dead_letter_delivery_failed'],4)
            self.assertEqual(detail['dead_letter_delivery_failed_24h'],3)
            # Grouped by target, most frequent first: a single noisy device stays distinguishable.
            self.assertEqual(detail['dead_letter_by_target_24h'],{'mac-codex-01':2,'mac-harness-01':1})
if __name__=='__main__':unittest.main()
