import base64
import concurrent.futures
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request

from scripts import ca_tool
from scripts.ocsp_responder import serve


class OcspResponderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ca = Path(cls.tmp.name) / 'ca'
        ca_tool.init_root(cls.ca, days=30)
        ca_tool.init_intermediate(cls.ca, days=30)
        ca_tool.issue(cls.ca, 'ocsp-signer', 'ocsp', 30, 'http://127.0.0.1:2560')
        result = ca_tool.issue(cls.ca, 'client-a', 'client', 30, 'http://127.0.0.1:2560')
        cls.cert = Path(result['cert'])
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            cls.port = probe.getsockname()[1]
        cls.server = serve(cls.ca, cls.port, workers=8)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _request_raw(self):
        with tempfile.TemporaryDirectory() as folder:
            req = Path(folder) / 'req.der'
            subprocess.run(['openssl', 'ocsp', '-issuer', str(self.ca / 'intermediate/intermediate.crt'),
                            '-cert', str(self.cert), '-reqout', str(req), '-no_nonce'],
                           check=True, capture_output=True)
            return req.read_bytes()

    def _request(self):
        raw = self._request_raw()
        url = f'http://127.0.0.1:{self.port}/' + urllib.parse.quote(base64.b64encode(raw).decode())
        return urllib.request.urlopen(url, timeout=10).read()

    def test_04_in_memory_signing_does_not_fork_openssl(self):
        from scripts.ocsp_responder import OcspSigner
        from unittest.mock import patch
        signer = OcspSigner(self.ca)
        raw = self._request_raw()
        with patch('scripts.ocsp_responder.subprocess.run') as fork:
            der = signer.sign(raw)
        fork.assert_not_called()
        self.assertGreater(len(der), 0)

    def _text(self, body):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'resp.der'
            path.write_bytes(body)
            return subprocess.run(['openssl', 'ocsp', '-respin', str(path), '-text', '-noverify'],
                                  capture_output=True, text=True).stdout

    def test_01_concurrent_requests(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            sizes = list(pool.map(lambda _: len(self._request()), range(12)))
        self.assertTrue(all(size > 0 for size in sizes))

    def test_02_revocation_is_immediate_and_get_form_works(self):
        self.assertIn('Cert Status: good', self._text(self._request()))
        ca_tool.revoke(self.ca, 'client-a')
        self.assertIn('revoked', self._text(self._request()))

    def test_03_bad_request_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f'http://127.0.0.1:{self.port}/not-base64!!', timeout=5)
        self.assertEqual(caught.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
