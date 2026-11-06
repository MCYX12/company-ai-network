"""The packaged device worker: fixed command, stdin question, bounded output."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('device_worker', ROOT / 'packaging/client/worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class DeviceWorkerTests(unittest.TestCase):
    def test_settings_defaults_then_worker_json_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(worker.settings(tmp)['prefix'], 'corp.dev.v1')
            (Path(tmp) / 'worker.json').write_text(json.dumps({'command': ['/bin/echo', 'hi']}))
            self.assertEqual(worker.settings(tmp)['command'], ['/bin/echo', 'hi'])

    def test_usable_actions_drops_unknown_and_unsupported(self):
        caps, actions = worker.usable_actions(['get_status', 'ask_peer', 'bogus'], ['ask_peer', 'execute_shell'])
        self.assertEqual(caps, ['get_status', 'ask_peer'])
        self.assertEqual(actions, ['ask_peer'])

    def test_question_goes_to_stdin_and_answer_comes_from_stdout(self):
        config = {'command': ['/usr/bin/python3', '-c', 'import sys;print("A:"+sys.stdin.read().strip())'],
                  'timeout_seconds': 30}
        code, data = worker.run_command(config, 'hello', time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['source'], 'device_worker')
        self.assertEqual(data['text'], 'A:hello')

    def test_shell_string_command_refused(self):
        code, _ = worker.run_command({'command': 'rm -rf /'}, 'q', time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')

    def test_empty_argument_allowed_but_blank_program_refused(self):
        # Same rule as app/harness_adapter.py: `--tools ''` (an explicit empty argument)
        # must be configurable, while a blank program must not be.
        code, data = worker.run_command({'command': ['/bin/echo', '', 'hi'], 'timeout_seconds': 30},
                                        'q', time.time() + 30)
        self.assertEqual(code, 'OK', data)
        code, _ = worker.run_command({'command': ['', 'hi'], 'timeout_seconds': 30}, 'q', time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(worker.preflight({'command': ['', 'hi']}), ('probe_failed', 'WORKER_COMMAND_INVALID'))

    def test_sandbox_wrapper_is_prepended(self):
        config = {'command': ['/usr/bin/python3', '-c', 'print("sandboxed")'],
                  'sandbox': ['/usr/bin/env'], 'timeout_seconds': 30}
        code, data = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'OK', data)
        self.assertEqual(data['text'], 'sandboxed')
        for bad in ('bwrap', [], ['']):
            code, _ = worker.run_command(dict(config, sandbox=bad), 'q', time.time() + 30)
            self.assertEqual(code, 'TOOL_UNAVAILABLE', bad)
            self.assertEqual(worker.preflight(dict(config, sandbox=bad)),
                             ('probe_failed', 'WORKER_SANDBOX_INVALID'), bad)

    def test_timeout_is_bounded(self):
        config = {'command': ['/usr/bin/python3', '-c', 'import time;time.sleep(30)'], 'timeout_seconds': 1}
        code, _ = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'EXECUTION_TIMEOUT')

    def test_oversize_output_refused(self):
        config = {'command': ['/usr/bin/python3', '-c', 'print("x"*20000)'], 'timeout_seconds': 30}
        code, _ = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'OUTPUT_TOO_LARGE')

    def test_env_is_allowlisted_not_inherited(self):
        config = {'command': ['/usr/bin/python3', '-c',
                              'import os;print(os.environ.get("WORKER_SECRET_XYZ","MISSING"))'],
                  'timeout_seconds': 30}
        with patch.dict(os.environ, {'WORKER_SECRET_XYZ': 'leak'}):
            code, data = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['text'], 'MISSING')

    def test_explicit_env_is_passed_to_child(self):
        config = {'command': ['/usr/bin/python3', '-c',
                              'import os;print(os.environ.get("WORKER_SECRET_XYZ","MISSING"))'],
                  'timeout_seconds': 30, 'env': {'WORKER_SECRET_XYZ': 'given'}}
        code, data = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'OK')
        self.assertEqual(data['text'], 'given')

    def test_missing_command_is_tool_unavailable(self):
        code, data = worker.run_command({'command': ['/nonexistent/bin/does-not-exist-xyz'],
                                         'timeout_seconds': 30}, 'q', time.time() + 30)
        self.assertEqual(code, 'TOOL_UNAVAILABLE')
        self.assertEqual(data['hint'], 'WORKER_COMMAND_NOT_FOUND')

    def test_stderr_tail_recorded_on_failure(self):
        config = {'command': ['/usr/bin/python3', '-c',
                              'import sys;sys.stderr.write("worker-boom");sys.exit(2)'],
                  'timeout_seconds': 30}
        code, data = worker.run_command(config, 'q', time.time() + 30)
        self.assertEqual(code, 'EXECUTION_FAILED')
        self.assertIn('worker-boom', data['stderr_tail'])

    def test_preflight_reports_ready_and_missing(self):
        self.assertEqual(worker.preflight({'command': ['/usr/bin/python3'], 'timeout_seconds': 30})[0], 'ready')
        self.assertEqual(worker.preflight({'command': ['/nonexistent/bin/x'], 'timeout_seconds': 30}),
                         ('not_found', 'WORKER_COMMAND_NOT_FOUND'))
        self.assertEqual(worker.preflight({'command': 'rm -rf /', 'timeout_seconds': 30})[0], 'probe_failed')
