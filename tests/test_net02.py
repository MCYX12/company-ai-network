import socket
import ssl
from pathlib import Path
import tempfile
import threading
import unittest

from scripts import bootstrap, net_precheck

CERT_DIR = Path(__file__).resolve().parents[1] / '.runtime'


class TlsServer:
    def __init__(self, cert, key):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(5)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(1)
            except Exception:
                pass

    def close(self):
        self.socket.close()


class Net02Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        CERT_DIR.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=CERT_DIR)
        cls.work = Path(cls.tmp.name)
        cls.runtime = cls.work / 'runtime'
        bootstrap.RUNTIME = cls.runtime
        bootstrap.main()
        cls.certs = cls.runtime / 'certs'
        cls.server = TlsServer(str(cls.certs / 'server.crt'), str(cls.certs / 'server.key'))

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.tmp.cleanup()

    def test_resolve_localhost(self):
        out = net_precheck.resolve('localhost')
        self.assertEqual(out['result'], 'resolved')
        self.assertIn('127.0.0.1', out['addresses'])

    def test_advertised_address_mismatch_is_flagged(self):
        out = net_precheck.node_check([{'host': 'localhost', 'port': self.server.port, 'address': '10.255.255.1'}])
        self.assertEqual(out['nodes'][0]['result'], 'MISMATCH')

    def test_tls_ok_and_wrong_ca_fails(self):
        ok = net_precheck.tls_probe('localhost', self.server.port, self.certs / 'ca.crt')
        self.assertEqual(ok['result'], 'tls_ok')
        self.assertTrue(ok['san'])
        other = self.work / 'other'
        bootstrap.RUNTIME = other
        bootstrap.main()
        bad = net_precheck.tls_probe('localhost', self.server.port, other / 'certs/ca.crt')
        self.assertEqual(bad['result'], 'tls_error')
        bootstrap.RUNTIME = self.runtime

    def test_node_check_ok_with_certificate(self):
        out = net_precheck.node_check([{'host': 'localhost', 'port': self.server.port, 'ca': str(self.certs / 'ca.crt')}])
        self.assertEqual(out['nodes'][0]['result'], 'OK')
        self.assertEqual(out['summary'], {'total': 1, 'pass': 1, 'fail': 0})


if __name__ == '__main__':
    unittest.main()
