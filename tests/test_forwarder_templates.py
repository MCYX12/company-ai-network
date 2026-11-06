from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / 'packaging/systemd/company-agent-forwarder.service'
PS1 = ROOT / 'packaging/windows/install_forwarder.ps1'


class ForwarderTemplateTests(unittest.TestCase):
    def test_systemd_template(self):
        text = SYSTEMD.read_text()
        for token in ['local_forwarder', '--listen-port', 'Restart=always', 'WantedBy=default.target']:
            self.assertIn(token, text)

    def test_windows_template(self):
        text = PS1.read_text()
        for token in ['local_forwarder', 'Register-ScheduledTask', 'endpoint.txt', 'tls://127.0.0.1:', 'AtStartup']:
            self.assertIn(token, text)
        self.assertNotIn('.sh', text)
        self.assertTrue(text.count('"') % 2 == 0)
        self.assertEqual(text.count('{'), text.count('}'))


if __name__ == '__main__':
    unittest.main()
