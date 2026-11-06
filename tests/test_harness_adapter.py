"""One-shot local harness adapter: fixed command, stdout answer, bounded output."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import harness_adapter


class HarnessAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name)
        self.configure(['/usr/bin/python3', '-c', 'import sys;print("ANS:"+sys.stdin.read().strip())'], 30)
        self.patcher = patch.object(harness_adapter, 'RUNTIME', self.runtime)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def configure(self, command, seconds):
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {
            'command': command, 'timeout_seconds': seconds}}}))

    def row(self, tool='harness'):
        return {'agent_id': 'ubuntu-harness-01', 'tool': tool, 'projects': ['demo-project'],
                'enabled': True, 'capabilities': ['get_status', 'ask_peer']}

    def test_is_harness_reads_configuration(self):
        self.assertTrue(harness_adapter.is_harness(self.row()))
        self.assertFalse(harness_adapter.is_harness({'tool': 'fixture'}))

    def test_execute_returns_stdout(self):
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['source'], 'local_harness')
        self.assertTrue(data['text'].startswith('ANS:'))

    def test_invalid_question_rejected(self):
        code, _ = harness_adapter.execute(self.row(), {'question': '', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'INVALID_REQUEST')

    def test_command_is_a_list_not_a_shell_string(self):
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {'command': 'rm -rf /'}}}))
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'HARNESS_COMMAND_INVALID')

    def test_empty_argument_allowed_but_blank_program_refused(self):
        # claude's documented no-tools form is `--tools ''`: one explicit empty argv element.
        # It carries no injection risk, so a harness must be able to configure it.
        self.configure(['/usr/bin/python3', '-c', 'import sys;print("ANS:"+sys.stdin.read().strip())', ''], 30)
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OK', data)
        # A blank program is still refused, and preflight reports it before any task arrives.
        self.configure(['', '-c', 'print(1)'], 30)
        self.assertEqual(harness_adapter.preflight('harness'), ('probe_failed', 'HARNESS_COMMAND_INVALID'))
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'HARNESS_COMMAND_INVALID')

    def test_deadline_in_the_past_expires(self):
        code, _ = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() - 10)
        self.assertEqual(code, 'EXPIRED')

    def test_slow_command_hits_timeout(self):
        self.configure(['/usr/bin/python3', '-c', 'import time;time.sleep(30)'], 1)
        code, _ = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'EXECUTION_TIMEOUT')

    def test_oversize_output_refused(self):
        self.configure(['/usr/bin/python3', '-c', 'print("x"*20000)'], 30)
        code, _ = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OUTPUT_TOO_LARGE')

    def test_failing_command_is_reported(self):
        self.configure(['/usr/bin/python3', '-c', 'import sys;sys.exit(3)'], 30)
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'EXECUTION_FAILED')
        self.assertEqual(data['exit_code'], 3)

    def test_failing_command_reports_stderr_tail(self):
        self.configure(['/usr/bin/python3', '-c', 'import sys;sys.stderr.write("boom-detail");sys.exit(2)'], 30)
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'EXECUTION_FAILED')
        self.assertIn('boom-detail', data['stderr_tail'])

    def test_child_env_is_allowlisted_not_inherited(self):
        with patch.dict(os.environ, {'HARNESS_SECRET_XYZ': 'leak'}):
            self.configure(['/usr/bin/python3', '-c',
                            'import os;print(os.environ.get("HARNESS_SECRET_XYZ","MISSING"))'], 30)
            code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['text'], 'MISSING')

    def test_explicit_env_is_passed_to_child(self):
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {
            'command': ['/usr/bin/python3', '-c',
                        'import os;print(os.environ.get("HARNESS_SECRET_XYZ","MISSING"))'],
            'timeout_seconds': 30,
            'env': {'HARNESS_SECRET_XYZ': 'given'}}}}))
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['text'], 'given')

    def test_missing_command_is_tool_unavailable(self):
        self.configure(['/nonexistent/bin/does-not-exist-xyz'], 30)
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'HARNESS_COMMAND_NOT_FOUND')

    def test_sandbox_wrapper_is_prepended(self):
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {
            'command': ['/usr/bin/python3', '-c', 'print("sandboxed")'],
            'timeout_seconds': 30, 'sandbox': ['/usr/bin/env']}}}))
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'OK', data)
        self.assertEqual(data['text'], 'sandboxed')

    def test_invalid_sandbox_is_refused_not_ignored(self):
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {
            'command': ['/usr/bin/python3', '-c', 'print(1)'], 'timeout_seconds': 30,
            'sandbox': 'bwrap'}}}))
        self.assertEqual(harness_adapter.preflight('harness'), ('probe_failed', 'HARNESS_SANDBOX_INVALID'))
        code, data = harness_adapter.execute(self.row(), {'question': 'hi', 'projects': ['demo-project']}, time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'HARNESS_SANDBOX_INVALID')

    def test_preflight_reports_ready_and_missing(self):
        self.assertEqual(harness_adapter.preflight('harness')[0], 'ready')
        self.configure(['/nonexistent/bin/does-not-exist-xyz'], 30)
        self.assertEqual(harness_adapter.preflight('harness'), ('not_found', 'HARNESS_COMMAND_NOT_FOUND'))


    def test_health_surfaces_a_harness_without_a_process_boundary(self):
        from app.healthcheck import local_checks
        # configure() writes a harness with neither cwd nor sandbox: it inherits the platform cwd.
        stage = next(c for c in local_checks(self.runtime) if c['check'] == 'harness_isolation')
        self.assertEqual(stage['status'], 'warn')
        self.assertIn('harness', stage['detail']['no_boundary'])
        (self.runtime / 'harness.json').write_text(json.dumps({'tools': {'harness': {
            'command': ['/bin/echo', 'hi'], 'timeout_seconds': 30, 'cwd': tempfile.gettempdir()}}}))
        stage = next(c for c in local_checks(self.runtime) if c['check'] == 'harness_isolation')
        self.assertEqual(stage['status'], 'ok')
        self.assertEqual(stage['detail']['with_boundary'], ['harness'])

    def test_health_is_ok_when_no_harness_is_configured(self):
        from app.healthcheck import local_checks
        (self.runtime / 'harness.json').unlink()
        stage = next(c for c in local_checks(self.runtime) if c['check'] == 'harness_isolation')
        self.assertEqual(stage['status'], 'ok')
        self.assertEqual(stage['detail']['harness_tools'], [])


if __name__ == '__main__':
    unittest.main()
