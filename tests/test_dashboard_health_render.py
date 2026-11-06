"""The admin health render must show string/numeric reasons and a single timestamp."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HealthRenderTests(unittest.TestCase):
    def test_refresh_health_renders_reasons_and_unique_time(self):
        text = (ROOT / 'web/app.js').read_text()
        start = text.index('async function refreshHealth(){')
        end = text.index('\n', start)
        fn = text[start:end]
        self.assertIn('checked_at', fn)
        self.assertNotIn('generated_at', fn)             # no second, contradictory timestamp
        self.assertIn("typeof det==='string'", fn)       # string detail is shown
        self.assertIn('Object.keys(det)', fn)            # structured numeric detail is shown
        self.assertIn('admission_problems', fn)
        self.assertIn('healthSyncBlock(d)', fn)          # refreshHealth calls the sync renderer
        sync_start = text.index('function healthSyncBlock(')
        sync_fn = text[sync_start:text.index('\n', sync_start)]
        self.assertIn('d.oidc_sync', sync_fn)            # sync status is rendered
        self.assertIn('last_error', sync_fn)


if __name__ == '__main__':
    unittest.main()
