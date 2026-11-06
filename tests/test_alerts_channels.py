import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest

from scripts import alerts

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class Collector(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        Collector.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class AlertChannelTests(unittest.TestCase):
    def test_webhook_delivery(self):
        Collector.received = []
        server = http.server.HTTPServer(('127.0.0.1', 0), Collector)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            delivered = alerts.notify({'alerts': [{'rule': 'x'}]}, webhook='http://127.0.0.1:%d/hook' % server.server_port)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(delivered['webhook'], 200)
        self.assertEqual(Collector.received[0]['alerts'][0]['rule'], 'x')

    def test_command_delivery(self):
        with tempfile.TemporaryDirectory(dir=CERT_DIR) as folder:
            out = Path(folder) / 'alert.json'
            delivered = alerts.notify({'alerts': []}, command='cat > %s' % out)
            self.assertEqual(delivered['command'], 0)
            self.assertEqual(json.loads(out.read_text())['alerts'], [])

    def test_handler_owner_is_attached(self):
        report = alerts.evaluate(CERT_DIR, health={'status': 'ok', 'checks': [
            {'check': 'disk_space', 'status': 'ok', 'detail': {'free_gib': 0.1}}]},
            limits={'handlers': {'disk_free_gib': 'ops-team'}, 'queue_seconds_max': 10 ** 9,
                    'run_seconds_max': 10 ** 9, 'auth_denied_total': 10 ** 9,
                    'project_age_seconds_max': 10 ** 9, 'renewal_failures': 10 ** 9,
                    'dead_letter_max': 10 ** 9, 'disk_free_gib_min': 2})
        self.assertEqual(report['alerts'][0]['rule'], 'disk_free_gib')
        self.assertEqual(report['alerts'][0]['owner'], 'ops-team')


if __name__ == '__main__':
    unittest.main()
