import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
from app.common import request
from app.text_task import execute_text

class TextTests(unittest.TestCase):
    row={'agent_id':'test-codex','tool':'codex'}
    def test_disabled_does_not_start_cli(self):
        with patch.dict(os.environ,{},clear=True),patch('app.text_task.inspect_tool') as probe:
            self.assertEqual(execute_text(self.row,{'prompt':'hello'},time.time()+30)[0],'CLOUD_TEST_DISABLED')
            probe.assert_not_called()
    def test_bad_inputs_do_not_start_cli(self):
        with patch.dict(os.environ,{'ALLOW_CLOUD_MODEL_TESTS':'1'}),patch('app.text_task.inspect_tool') as probe:
            for payload in [{'prompt':' '},{'prompt':'a'*4001},{'prompt':'ok','command':'rm'}]:
                self.assertEqual(execute_text(self.row,payload,time.time()+30)[0],'INVALID_REQUEST')
            probe.assert_not_called()
    def test_expired_does_not_execute(self):
        with patch.dict(os.environ,{'ALLOW_CLOUD_MODEL_TESTS':'1'}),patch('app.text_task.inspect_tool',return_value={'adapter_status':'status_only'}),patch('app.text_task.subprocess.Popen') as spawn:
            self.assertEqual(execute_text(self.row,{'prompt':'ok'},time.time()-1)[0],'EXPIRED')
            spawn.assert_not_called()
    def _fake_cli(self,stdout=b'',stderr=b'',returncode=1):
        class Proc:
            def __init__(self,argv,**kwargs):
                self.returncode=returncode;self.pid=4242
                if stderr and kwargs.get('stderr') is not None:kwargs['stderr'].write(stderr)
                if stdout and kwargs.get('stdout') is not None:kwargs['stdout'].write(stdout)
            def communicate(self,data=None,timeout=None):return (b'',b'')
        return Proc
    def _run(self,proc,env):
        import io,contextlib
        out=io.StringIO()
        with patch.dict(os.environ,{'ALLOW_CLOUD_MODEL_TESTS':'1',**env}),\
             patch('app.text_task.inspect_tool',return_value={'adapter_status':'status_only','executable':'/bin/true'}),\
             patch('app.text_task.subprocess.Popen',proc),contextlib.redirect_stdout(out):
            return (*execute_text(self.row,{'prompt':'hello'},time.time()+30),out.getvalue())
    def test_cli_failure_logs_the_exit_code_even_with_empty_stderr(self):
        code,data,logged=self._run(self._fake_cli(),{})
        self.assertEqual(code,'EXECUTION_FAILED')
        self.assertEqual(data['exit_code'],1)
        self.assertIn('CLI exited 1',logged)
        self.assertIn('no error signal',logged)
        self.assertNotIn('diagnostic',data)
    def test_stdout_only_error_is_used_only_when_opted_in(self):
        err=b'API Error: API returned an empty or malformed response (HTTP 200)\n'
        code,data,logged=self._run(self._fake_cli(stdout=err),{'POC_CLI_STDOUT_DIAGNOSTICS':'1'})
        self.assertEqual(code,'EXECUTION_FAILED')
        self.assertIn('HTTP 200',data['diagnostic'])
        self.assertIn('HTTP 200',logged)
        code,data,logged=self._run(self._fake_cli(stdout=err),{'POC_CLI_STDOUT_DIAGNOSTICS':'0'})
        self.assertNotIn('diagnostic',data)
        self.assertNotIn('HTTP 200',logged)
    def test_stderr_signal_is_still_preferred_and_reported(self):
        code,data,logged=self._run(self._fake_cli(stderr=b'ERROR: quota exceeded\n'),{})
        self.assertEqual(data['diagnostic'],'ERROR: quota exceeded')
        self.assertIn('quota exceeded',logged)
    def test_schema_requires_prompt_only_for_text(self):
        schema=json.loads(Path('schemas/query.json').read_text());v=Draft202012Validator(schema)
        b=request();b['action']='submit_task';b['payload']={'operation':'generate_text','ttl_seconds':120,'idempotency_key':'test'}
        self.assertFalse(v.is_valid(b))
        b['payload']['prompt']='hello';self.assertTrue(v.is_valid(b))
        b['payload']['operation']='get_status';self.assertFalse(v.is_valid(b))
        b['payload'].pop('prompt');self.assertTrue(v.is_valid(b))
if __name__=='__main__':unittest.main()
