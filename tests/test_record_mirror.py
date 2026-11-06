"""Record mirror: transient transfer failures are retried and verified, never forgotten."""
import tempfile
import unittest
from pathlib import Path

from scripts import record_mirror


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.record = self.root / 'demo-project.json'
        self.record.write_text('{"project_id":"demo-project"}')
        self.targets = self.root / 'push-targets.txt'
        self.targets.write_text('mac-1|/remote/records\nmac-2|/remote/records\n')
        self.state = self.root / 'mirror-state.json'
        self.digest = record_mirror.local_digest(self.record)

    def tearDown(self):
        self.tmp.cleanup()

    def runner(self, scp_ok=True, place_ok=True, remote='match'):
        record = self.record

        def run(command, timeout=30):
            if command[0] == 'scp':
                return (0, '') if scp_ok else (1, 'scp down')
            text = ' '.join(command)
            if 'sha256sum' in text:
                if remote == 'match':
                    return (0, record_mirror.local_digest(record))
                return (0, remote)          # wrong digest
            return (0, '') if place_ok else (1, 'place failed')
        return run

    def test_success_is_verified_and_clears_pending(self):
        result = record_mirror.mirror(self.record, record_mirror.parse_targets(self.targets),
                                      self.state, runner=self.runner())
        self.assertTrue(result['synced'])
        self.assertEqual([r['status'] for r in result['report']], ['pushed', 'pushed'])
        self.assertEqual(record_mirror.load_state(self.state)['pending'], {})

    def test_transient_failure_is_pending_then_retried_and_cleared(self):
        targets = record_mirror.parse_targets(self.targets)
        first = record_mirror.mirror(self.record, targets, self.state, runner=self.runner(scp_ok=False))
        self.assertFalse(first['synced'])
        self.assertEqual(first['pending'], ['mac-1', 'mac-2'])
        # The local record did not change, but the next cycle still retries and verifies.
        second = record_mirror.mirror(self.record, targets, self.state, runner=self.runner())
        self.assertTrue(second['synced'])
        self.assertEqual(record_mirror.load_state(self.state)['pending'], {})

    def test_remote_digest_mismatch_keeps_pending(self):
        result = record_mirror.mirror(self.record, record_mirror.parse_targets(self.targets),
                                      self.state, runner=self.runner(remote='deadbeef'))
        self.assertFalse(result['synced'])
        self.assertEqual(result['pending'], ['mac-1', 'mac-2'])
        self.assertIn('mismatch', result['report'][0]['detail'])

    def test_place_failure_keeps_pending(self):
        result = record_mirror.mirror(self.record, record_mirror.parse_targets(self.targets),
                                      self.state, runner=self.runner(place_ok=False))
        self.assertFalse(result['synced'])
        self.assertEqual(result['pending'], ['mac-1', 'mac-2'])


if __name__ == '__main__':
    unittest.main()
