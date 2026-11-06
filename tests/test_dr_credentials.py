import json
from pathlib import Path
import tempfile
import unittest

from app.task_store import TaskStore
from scripts import runtime_backup

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class DrCredentialsTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.work = Path(self.tmp.name)
        self.runtime = self.work / 'runtime'
        self.runtime.mkdir()
        TaskStore(self.runtime / 'tasks.sqlite3').submit('ag-a', 'ag-b', 'k', 'ask_peer', {'question': 'x'}, 300)
        (self.runtime / 'certs').mkdir()
        (self.runtime / 'certs/ag-a.key').write_text('PRIVATE KEY MATERIAL\n')
        (self.runtime / 'certs/ag-a.crt').write_text('CERTIFICATE MATERIAL\n')
        (self.runtime / 'certs/ENTERPRISE_CA.json').write_text(json.dumps({'format': 1, 'ca_dir': 'x'}))
        (self.runtime / 'nats.conf').write_text('{"listen":"127.0.0.1:1"}')
        (self.runtime / 'endpoint.txt').write_text('tls://localhost:1\n')
        (self.runtime / 'enterprise-ca/intermediate').mkdir(parents=True)
        (self.runtime / 'enterprise-ca/intermediate/intermediate.key').write_text('INTERMEDIATE KEY\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_credentials_backup_restore_roundtrip(self):
        snapshot = self.work / 'snap-cred'
        manifest = runtime_backup.backup(self.runtime, snapshot, include_credentials=True, include_ca=True)
        self.assertTrue(manifest['contains_private_keys'])
        runtime_backup.verify(snapshot)
        destination = self.work / 'restore-cred'
        report = runtime_backup.restore(snapshot, destination)
        self.assertTrue(report['network_credentials_restored'])
        self.assertEqual((destination / 'certs/ag-a.key').read_text(), 'PRIVATE KEY MATERIAL\n')
        self.assertEqual((destination / 'enterprise-ca/intermediate/intermediate.key').read_text(), 'INTERMEDIATE KEY\n')
        self.assertEqual((destination / 'endpoint.txt').read_text(), 'tls://localhost:1\n')

    def test_ledger_only_snapshot_has_no_keys(self):
        snapshot = self.work / 'snap-ledger'
        manifest = runtime_backup.backup(self.runtime, snapshot)
        self.assertFalse(manifest['contains_private_keys'])
        report = runtime_backup.restore(snapshot, self.work / 'restore-ledger')
        self.assertFalse(report['network_credentials_restored'])


if __name__ == '__main__':
    unittest.main()
