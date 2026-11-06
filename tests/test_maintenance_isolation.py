import json,tempfile,unittest
from pathlib import Path
from app import maintenance,agent

class Tests(unittest.TestCase):
    def test_cancellation_while_probe_runs_is_not_overwritten_by_timeout(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from scripts import maintenance as cli
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);maintenance.begin(['peer'],runtime=root)
            def probe(*_):maintenance.cancel(runtime=root);return []
            args=SimpleNamespace(runtime=root,identity='caller',endpoint=None,interval=0,stop=False,force=False,allow_partial_targets=False)
            with patch.object(cli.maintenance,'RUNTIME',root),patch.object(cli,'local_workers',return_value=(set(),False)),patch.object(cli,'draining_targets',side_effect=probe):
                self.assertEqual(cli.cmd_run(args),0)
            self.assertEqual(maintenance.read(root)['state'],'cancelled');self.assertFalse((root/'maintenance').exists())
    def test_selected_target_drains_without_freezing_other_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);maintenance.begin(['windows-probe-01'],runtime=root)
            self.assertTrue(maintenance.requested('windows-probe-01',root))
            self.assertFalse(maintenance.requested('ag-a',root))
            self.assertFalse(agent.maintenance_requested(root,'mac-harness-01'))
            maintenance.cancel(runtime=root)
            self.assertFalse(maintenance.requested('windows-probe-01',root))
    def test_local_portable_marker_invalid_or_missing_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'maintenance').write_text('owned marker')
            self.assertTrue(maintenance.requested('peer',root))
            for value in ({'targets':[]},{'targets':[True]},{'targets':['peer']}):
                (root/'maintenance.json').write_text(json.dumps(value))
                self.assertTrue(maintenance.requested('peer',root))
            (root/'maintenance.json').write_text(json.dumps({'targets':['other']}))
            self.assertFalse(maintenance.requested('peer',root))
