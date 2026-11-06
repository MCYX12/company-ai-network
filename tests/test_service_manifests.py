import plistlib
import unittest
from scripts.service_manifests import render,unit_quote

class ServiceManifestTests(unittest.TestCase):
    def test_linux_stops_children_and_restarts_failures(self):
        services=render('ubuntu','/home/test/桌面/a project')
        self.assertEqual(set(services),{'company-agent-stack.service','company-agent-dashboard.service','company-agent-healthcheck.service','company-agent-healthcheck.timer','company-agent-ocsp.service','company-agent-renew.service','company-agent-renew.timer','company-agent-ocsp-watchdog.service','company-agent-ocsp-watchdog.timer','company-agent-retention.service','company-agent-retention.timer','company-agent-record-refresh.service','company-agent-record-refresh.timer'})
        for name in ['company-agent-stack.service','company-agent-dashboard.service']:
            text=services[name].decode();self.assertIn('KillMode=mixed',text);self.assertIn('Restart=on-failure',text);self.assertIn('UMask=0077',text)
        self.assertIn('run_healthcheck.sh',services['company-agent-healthcheck.service'].decode())
        self.assertIn('WantedBy=timers.target',services['company-agent-healthcheck.timer'].decode())
        self.assertIn('run_ocsp_responder.sh',services['company-agent-ocsp.service'].decode())
        self.assertIn('Restart=always',services['company-agent-ocsp.service'].decode())
        self.assertNotIn(b'ALLOW_CLOUD_MODEL_TESTS',b''.join(services.values()))
    def test_mac_cloud_default_off_and_no_shell_interpolation(self):
        services=render('mac','/Users/test/Downloads/company-agent-device',account_home='/Users/test')
        device=plistlib.loads(services['com.companyagent.device.plist'])
        self.assertEqual(device['ProgramArguments'],['/bin/bash','/Users/test/Downloads/company-agent-device/start-device.sh'])
        self.assertTrue(device['KeepAlive']);self.assertEqual(device['EnvironmentVariables']['ALLOW_CLOUD_MODEL_TESTS'],'0')
        self.assertTrue(device['EnvironmentVariables']['PATH'].startswith('/Users/test/.local/bin:'))
        enabled=plistlib.loads(render('mac','/Users/test/Downloads/company-agent-device',True)['com.companyagent.device.plist'])
        self.assertEqual(enabled['EnvironmentVariables']['ALLOW_CLOUD_MODEL_TESTS'],'1')
    def test_paths_reject_control_and_relative(self):
        with self.assertRaises(ValueError):render('ubuntu','relative')
        with self.assertRaises(ValueError):render('ubuntu','/x\nExecStart=bad')
        self.assertEqual(unit_quote('/tmp/a%b$c'), '"/tmp/a%%b$$c"')
        with self.assertRaises(ValueError):render('ubuntu','/tmp/a',True)
if __name__=='__main__':unittest.main()
