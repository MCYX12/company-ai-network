"""Unreadable OIDC token dir is no_input, and healthcheck maps it to fail."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import healthcheck
from scripts import oidc_sync_runner


class NoInputTests(unittest.TestCase):
    def test_invalid_sync_snapshot_is_failed_health_and_never_breaks_runner(self):
        from unittest.mock import patch
        from app.dashboard import DashboardServer
        from tests.test_dashboard import FakeBridge
        import threading,http.client
        invalid=[None,[],{'status':'ok','failed':True,'checked_at':1},
                 {'status':'ok','failed':0,'checked_at':'bad'},
                 {'status':'ok','failed':0,'checked_at':1,'last_success':'bad'},
                 {'status':'unknown','failed':0,'checked_at':1}]
        with tempfile.TemporaryDirectory() as folder:
            runtime=Path(folder);(runtime/'health').mkdir();tokens=runtime/'tokens';tokens.mkdir()
            server=DashboardServer(('127.0.0.1',0),FakeBridge())
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                for value in invalid:
                    with self.subTest(value=value):
                        file=runtime/'health/oidc-sync.json';file.write_text(json.dumps(value))
                        sync=next(c for c in healthcheck.local_checks(runtime) if c['check']=='oidc_sync')
                        self.assertEqual(sync['status'],'fail')
                        with patch('app.common.RUNTIME',runtime):
                            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                            conn.request('GET','/api/health',headers={'X-Dashboard-Token':server.token})
                            reply=conn.getresponse();body=json.loads(reply.read());conn.close()
                        self.assertEqual(body['oidc_sync']['status'],'failed')
                        self.assertEqual(body['oidc_sync']['last_error'],'OIDC_SNAPSHOT_INVALID')
                        snapshot=oidc_sync_runner.run(runtime,tokens,apply=True)
                        self.assertEqual(snapshot['status'],'empty')
                        self.assertIsNone(snapshot['last_success'])
                        self.assertEqual(snapshot['previous_snapshot_error'],'OIDC_SNAPSHOT_INVALID')
            finally:server.shutdown();server.server_close();thread.join()

    def test_unreadable_directory_is_no_input(self):
        if os.geteuid() == 0:
            self.skipTest('root bypasses directory permissions')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            tokens = folder / 'tokens'
            tokens.mkdir()
            try:
                os.chmod(tokens, 0)
                snapshot = oidc_sync_runner.run(folder / 'runtime', tokens, apply=True)
                self.assertEqual(snapshot['status'], 'no_input')
                self.assertIsNotNone(snapshot['input_error'])
                self.assertIsNone(snapshot['last_success'])
            finally:
                os.chmod(tokens, 0o700)

    def test_inaccessible_parent_directory_is_no_input(self):
        if os.geteuid() == 0:
            self.skipTest('root bypasses directory permissions')
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            parent = folder / 'parent'
            tokens = parent / 'tokens'
            tokens.mkdir(parents=True)
            try:
                os.chmod(parent, 0)          # is_dir()/iterdir() raise on the inaccessible parent
                snapshot = oidc_sync_runner.run(folder / 'runtime', tokens, apply=True)
                self.assertEqual(snapshot['status'], 'no_input')
                self.assertIsNotNone(snapshot['input_error'])
            finally:
                os.chmod(parent, 0o700)

    def test_healthcheck_marks_no_input_as_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            (runtime / 'health').mkdir()
            (runtime / 'health' / 'oidc-sync.json').write_text(json.dumps(
                {'status': 'no_input', 'failed': 0, 'applied': 0, 'checked_at': 0,
                 'input_error': 'token directory not found', 'last_success': None}))
            checks = healthcheck.local_checks(runtime)
            check = next(c for c in checks if c['check'] == 'oidc_sync')
            self.assertEqual(check['status'], 'fail')


if __name__ == '__main__':
    unittest.main()
