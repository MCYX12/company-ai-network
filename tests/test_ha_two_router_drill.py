"""A revocation on the shared authority is observed by a separately started Router process."""
import tempfile
import unittest
from pathlib import Path

from scripts import ha_two_router_drill


class HaTwoRouterDrillTests(unittest.TestCase):
    def test_separate_process_observes_the_shared_revocation(self):
        with tempfile.TemporaryDirectory() as folder:
            result = ha_two_router_drill.run(Path(folder))
        self.assertTrue(result['stale_local_policy'])      # the local copy really does allow
        self.assertFalse(result['shared_revoked_policy'])
        self.assertFalse(result['observed_policy'])        # shared authority wins over local
        self.assertFalse(result['observed_facade'])
        self.assertFalse(result['observed_approved'])
        self.assertTrue(result['agreement'])


if __name__ == '__main__':
    unittest.main()
