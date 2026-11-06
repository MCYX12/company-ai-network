import importlib.util
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dsh_adapter', ROOT / 'packaging/client/dsh_desktop_adapter.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class HarnessAdapterTests(unittest.TestCase):
    def test_only_complete_final_is_returned(self):
        stream = '\n'.join(json.dumps(x) for x in [
            {'type': 'thinking', 'text': 'private reasoning'},
            {'type': 'text', 'text': 'partial'},
            {'type': 'final', 'text': '中文回答'}])
        self.assertEqual(adapter.final_text(stream), '中文回答')

    def test_partial_error_and_tool_events_fail_closed(self):
        for events in [[], [{'type': 'text', 'text': 'partial'}],
                       [{'type': 'final', 'text': ''}],
                       [{'type': 'final', 'text': 'x'}, {'type': 'tool_call'}],
                       [{'type': 'final', 'text': 'x'}, {'type': 'error'}],
                       [{'type': 'final', 'text': 'x'}] * 2]:
            with self.subTest(events=events), self.assertRaises(ValueError):
                adapter.final_text('\n'.join(json.dumps(x) for x in events))

    def test_profile_failure_prevents_model_call(self):
        with patch.object(adapter, 'check', side_effect=ValueError('tools enabled')), patch.object(adapter, 'capture') as capture:
            with self.assertRaises(ValueError):
                adapter.answer(Path('app'), Path('own'), '问题')
            capture.assert_not_called()

    def test_prompt_is_stdin_and_session_is_fresh(self):
        prompt = '问题 " & $(whoami) 中文'
        with patch.object(adapter, 'check', return_value=(['exe', 'cli'], {}, Path('own'), {})), patch.object(adapter, 'capture', return_value=json.dumps({'type': 'final', 'text': '答案'})) as capture:
            self.assertEqual(adapter.answer(Path('app'), Path('own'), prompt), '答案')
            argv, supplied, *_ = capture.call_args.args
            self.assertEqual(argv, ['exe', 'cli', '--json', '-'])
            self.assertEqual(supplied, adapter.TEXT_ONLY_RULES+prompt)

    def test_timeout_terminates_child_tree(self):
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired('exe', 1), None]
        with patch.object(adapter.subprocess, 'Popen', return_value=process), patch.object(adapter, 'terminate_process_tree') as terminate:
            with self.assertRaises(TimeoutError):
                adapter.capture(['exe'], '问题', {}, '.', adapter.time.monotonic() + 1)
            terminate.assert_called_once_with(process)

    def test_nonzero_never_returns_partial_output(self):
        process = Mock(returncode=1)
        with patch.object(adapter.subprocess, 'Popen', return_value=process):
            with self.assertRaises(ValueError):
                adapter.capture(['exe'], '', {}, '.', adapter.time.monotonic() + 1)

    def test_unverified_version_is_rejected_before_config_and_model(self):
        with patch.object(adapter, 'runtime', return_value=(['exe'], 'cli', 'helper', {}, Path('own'))), patch.object(adapter, 'capture', return_value='0.3.0') as capture:
            with self.assertRaises(ValueError):
                adapter.check(Path('app'), Path('own'), adapter.time.monotonic() + 10)
            self.assertEqual(capture.call_count, 1)

    def test_existing_home_cannot_be_overwritten(self):
        with patch.object(adapter, 'runtime', return_value=(['exe'], 'cli', 'helper', {}, ROOT)), patch.object(adapter, 'capture') as capture:
            with self.assertRaises(ValueError):
                adapter.prepare(Path('app'), ROOT, adapter.time.monotonic() + 10)
            capture.assert_not_called()

    def test_raw_tool_markup_cannot_be_returned_as_success(self):
        for text in ['<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="bash">ls</｜｜DSML｜｜ invoke>',
                     '<tool_call>Read()</tool_call>', '<function_calls>open()</function_calls>']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                adapter.final_text(json.dumps({'type':'final','text':text}))
