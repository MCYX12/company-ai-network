import json
from pathlib import Path
import tempfile
import time
import unittest

from app import project_state
from app.project_records import read_progress
from scripts import refresh_summaries

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


def record(updated_at):
    return {'schema_version': 1, 'project_id': 'demo-project', 'source_id': 's', 'updated_at': updated_at,
            'summary': 'x', 'completed': [], 'blockers': [], 'evidence': []}


class ProjectStateTests(unittest.TestCase):
    def test_snapshot_windows(self):
        now = 1_000_000.0
        def at(seconds_ago):
            from datetime import datetime, timezone
            return datetime.fromtimestamp(now - seconds_ago, tz=timezone.utc).isoformat()
        self.assertEqual(project_state.snapshot(record(at(60)), now=now)['freshness'], 'current')
        self.assertEqual(project_state.snapshot(record(at(12 * 3600)), now=now)['freshness'], 'stale')
        self.assertEqual(project_state.snapshot(record(at(10 * 86400)), now=now)['freshness'], 'expired')
        self.assertEqual(project_state.snapshot({'updated_at': 'not-a-time'}, now=now)['freshness'], 'unknown')

    def test_read_progress_exposes_snapshot(self):
        with tempfile.TemporaryDirectory(dir=CERT_DIR) as folder:
            runtime = Path(folder)
            (runtime / 'project-records').mkdir()
            from datetime import datetime, timezone
            updated = datetime.now(timezone.utc).isoformat()
            (runtime / 'project-records/demo-project.json').write_text(json.dumps(record(updated)))
            import app.project_records as pr
            original = pr.RUNTIME
            pr.RUNTIME = runtime
            try:
                code, data = read_progress({'agent_id': 'peer-x', 'projects': ['demo-project']}, {'project_id': 'demo-project'})
            finally:
                pr.RUNTIME = original
        self.assertEqual(code, 'OK')
        self.assertEqual(data['freshness'], 'current')
        self.assertEqual(data['snapshot_at'], updated)
        self.assertIn('freshness_note', data)

    def test_refresh_summaries_scans_and_marks(self):
        with tempfile.TemporaryDirectory(dir=CERT_DIR) as folder:
            runtime = Path(folder)
            (runtime / 'project-records').mkdir()
            (runtime / 'project-records/demo-project.json').write_text(json.dumps(record('2020-01-01T00:00:00+00:00')))
            rows = refresh_summaries.scan(runtime)
            self.assertEqual(rows[0]['freshness'], 'expired')
            self.assertTrue(rows[0]['refresh_due'])
            self.assertEqual(refresh_summaries.mark(runtime, rows), 1)
            self.assertTrue((runtime / 'project-records/demo-project.refresh.json').exists())


if __name__ == '__main__':
    unittest.main()
