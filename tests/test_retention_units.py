from pathlib import Path
import unittest

from scripts.service_manifests import render


class RetentionUnitsTests(unittest.TestCase):
    def test_render_includes_retention_service_and_timer(self):
        units = render('ubuntu', Path('/opt/company-agent'))
        self.assertIn('company-agent-retention.service', units)
        self.assertIn('company-agent-retention.timer', units)
        service = units['company-agent-retention.service'].decode()
        timer = units['company-agent-retention.timer'].decode()
        self.assertIn('scripts.retention', service)
        self.assertIn('--apply', service)
        self.assertIn('--runtime', service)
        self.assertIn('OnCalendar=daily', timer)
        self.assertIn('company-agent-retention.service', timer)
        self.assertIn('Persistent=true', timer)


if __name__ == '__main__':
    unittest.main()
