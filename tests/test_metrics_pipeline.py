from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.task_store import TaskStore
from scripts import metrics_export

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class MetricsPipelineTests(unittest.TestCase):
    def setUp(self):
        CERT_DIR.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        self.runtime = Path(self.tmp.name)
        store = TaskStore(self.runtime / 'tasks.sqlite3')
        task = store.submit('ag-a', 'peer-x', 'k', 'ask_peer', {'question': 'x'}, 600)
        store.sent(task['task_id'])
        store.finish(task['task_id'], {'code': 'OK', 'data': {}})
        with sqlite3.connect(self.runtime / 'tasks.sqlite3') as db:
            db.execute("UPDATE task_audit SET timestamp=100 WHERE task_id=? AND event='accepted'", (task['task_id'],))
            db.execute("UPDATE task_audit SET timestamp=110 WHERE task_id=? AND event='dispatched'", (task['task_id'],))
            db.execute("UPDATE task_audit SET timestamp=130 WHERE task_id=? AND event='succeeded'", (task['task_id'],))
            db.execute("INSERT INTO task_audit(task_id,event,timestamp) VALUES('x','FORBIDDEN',140)")
        (self.runtime / 'project-records').mkdir()
        (self.runtime / 'project-records/demo.json').write_text('{"updated_at": "2020-01-01T00:00:00+00:00"}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_pipeline_metrics(self):
        lines = metrics_export.pipeline_metrics(self.runtime, {'renewal': {'failures': 2}})
        text = '\n'.join(lines)
        self.assertIn('company_agent_tasks_total{state="succeeded"} 1', text)
        self.assertIn('company_agent_queue_seconds_avg 10.000', text)
        self.assertIn('company_agent_run_seconds_avg 20.000', text)
        self.assertIn('company_agent_auth_denied_total 1', text)
        self.assertIn('company_agent_renewal_failures 2', text)
        self.assertIn('company_agent_project_age_seconds{project="demo"}', text)

    def test_render_includes_pipeline(self):
        text = metrics_export.render({'status': 'ok'}, self.runtime)
        self.assertIn('company_agent_health_status 1', text)
        self.assertIn('company_agent_queue_seconds_max 10.000', text)


if __name__ == '__main__':
    unittest.main()
