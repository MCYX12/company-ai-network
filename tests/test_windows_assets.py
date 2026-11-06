import subprocess
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / 'scripts/install_windows.ps1'
SELFCHECK = ROOT / 'scripts/selfcheck.ps1'


def balanced(text):
    return (text.count('{') == text.count('}') and text.count('(') == text.count(')')
            and text.count('"') % 2 == 0)


class WindowsAssetTests(unittest.TestCase):
    def test_installer_has_required_steps(self):
        text = INSTALL.read_text()
        for token in ['PythonPath', 'install.py', 'PackageZip', 'EnrollmentZip', 'client.json', 'certs', 'icacls', 'Expand-Archive',
                      'New-ScheduledTaskAction', 'Register-ScheduledTask', 'selfcheck.ps1']:
            self.assertIn(token, text, token)
        self.assertTrue(balanced(text))

    def test_selfcheck_has_exit_codes_and_doctor(self):
        text = SELFCHECK.read_text()
        for token in ['client.py', '--doctor', 'LASTEXITCODE', 'ConvertFrom-Json', 'exit 1', 'exit 2', 'exit 0']:
            self.assertIn(token, text, token)
        self.assertTrue(balanced(text))

    def test_windows_scripts_do_not_call_bash(self):
        for path in [INSTALL, SELFCHECK]:
            text = path.read_text()
            self.assertNotIn('.sh', text, path.name)
            self.assertNotIn('#!/bin/bash', text, path.name)

    def test_client_entry_points_import_on_this_platform(self):
        result = subprocess.run([sys.executable, '-c', 'import app.common, app.peer_client, app.agent, app.device'],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_hardcoded_posix_paths_in_app(self):
        offenders = []
        for path in (ROOT / 'app').glob('*.py'):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if any(token in line for token in ["'/tmp", '"/tmp', "'/etc", '"/etc', "'/var", '"/var']):
                    offenders.append(f'{path.name}:{number}')
        self.assertEqual(offenders, [])


if __name__ == '__main__':
    unittest.main()
