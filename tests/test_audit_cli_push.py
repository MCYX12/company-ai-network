"""The audit CLI must exit non-zero when the anchor upload fails."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AuditCliPushTests(unittest.TestCase):
    def _run(self, exit_code):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            scp = folder / 'scp'
            scp.write_text('#!/bin/sh\nexit %d\n' % exit_code)
            scp.chmod(0o755)
            audit = folder / 'audit.jsonl'
            audit.write_text('{"a": 1}\n')
            out = folder / 'anchor.json'
            env = dict(os.environ)
            env['PATH'] = str(folder) + os.pathsep + env.get('PATH', '')
            proc = subprocess.run([sys.executable, '-m', 'scripts.audit_chain', 'export',
                                   '--file', str(audit), '--out', str(out), '--push', 'fixture:/anchor'],
                                  cwd=str(ROOT), env=env, capture_output=True, text=True)
            anchor = out.read_bytes() if out.exists() else None
            return proc, anchor

    def test_push_failure_is_nonzero_and_keeps_the_local_anchor(self):
        proc, anchor = self._run(23)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(anchor)                       # the local anchor is kept
        self.assertEqual(json.loads(anchor)['algorithm'], 'sha256-chain')
        self.assertEqual(json.loads(proc.stdout)['pushed']['returncode'], 23)

    def test_push_success_is_zero(self):
        proc, anchor = self._run(0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == '__main__':
    unittest.main()
