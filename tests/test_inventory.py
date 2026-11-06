import subprocess
import unittest
from unittest.mock import patch
from app.inventory import inspect_tool
from app.adapters import execute

class InventoryTests(unittest.TestCase):
    @patch('app.inventory.locate',return_value=None)
    @patch('app.inventory.subprocess.run')
    def test_missing_does_not_run(self,run,locate):
        result=inspect_tool('codex')
        self.assertEqual(result['adapter_status'],'not_found');run.assert_not_called()

    @patch('app.inventory.locate',return_value='/opt/cli/codex')
    @patch('app.inventory.subprocess.run')
    def test_codex_fingerprint_and_fixed_argv(self,run,locate):
        run.return_value=subprocess.CompletedProcess([],0,'codex-cli 0.153.4\n')
        result=inspect_tool('codex')
        self.assertEqual(result['version'],'0.153.4')
        self.assertEqual(result['capabilities'],['get_status'])
        self.assertEqual(run.call_args.args[0],['/opt/cli/codex','--version'])
        self.assertNotIn('shell',run.call_args.kwargs)

    @patch('app.inventory.locate',return_value='/opt/cli/claude')
    @patch('app.inventory.subprocess.run')
    def test_claude_fingerprint(self,run,locate):
        run.return_value=subprocess.CompletedProcess([],0,'2.1.283 (Claude Code)\n')
        self.assertEqual(inspect_tool('claude-code')['version'],'2.1.283')

    @patch('app.inventory.locate',return_value='/opt/cli/codex')
    @patch('app.inventory.subprocess.run')
    def test_wrong_program_not_matched(self,run,locate):
        run.return_value=subprocess.CompletedProcess([],0,'some other program 0.153.4\n')
        result=inspect_tool('codex')
        self.assertEqual(result['adapter_status'],'unrecognized_version')
        self.assertEqual(result['capabilities'],[])

    @patch('app.inventory.locate',return_value='/opt/cli/codex')
    @patch('app.inventory.subprocess.run',side_effect=subprocess.TimeoutExpired('codex',2))
    def test_timeout_no_capabilities(self,run,locate):
        result=inspect_tool('codex')
        self.assertEqual(result['adapter_status'],'probe_timeout')
        self.assertEqual(result['capabilities'],[])

    @patch('app.inventory.locate',return_value='/opt/cli/hermes')
    @patch('app.inventory.subprocess.run')
    def test_unsupported_tool_is_not_executed(self,run,locate):
        self.assertEqual(inspect_tool('hermes')['adapter_status'],'not_integrated')
        run.assert_not_called()

    @patch('app.inventory.locate',return_value='/opt/cli/codex')
    @patch('app.inventory.subprocess.run')
    def test_paths_only_no_execution(self,run,locate):
        self.assertEqual(inspect_tool('codex',False)['adapter_status'],'not_probed')
        run.assert_not_called()

    @patch('app.adapters.inspect_tool')
    def test_no_fake_progress_or_command_injection(self,probe):
        row={'agent_id':'ubuntu-codex','tool':'codex'}
        self.assertEqual(execute(row,'get_project_progress',{})[0],'FORBIDDEN')
        for action in ['execute_shell','get_status; echo injected']:
            self.assertEqual(execute(row,action,{})[0],'UNSUPPORTED_ACTION')
        probe.assert_not_called()

    @patch('app.adapters.inspect_tool',return_value={'adapter_status':'not_found','capabilities':[]})
    def test_uninstalled_after_enrollment_fails_honestly(self,probe):
        code,data=execute({'agent_id':'ubuntu-codex','tool':'codex'},'get_status',{})
        self.assertEqual(code,'TOOL_UNAVAILABLE');self.assertFalse(data['synthetic'])

if __name__=='__main__':unittest.main()
