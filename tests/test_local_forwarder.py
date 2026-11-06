import socket
import threading
import time
import unittest

from scripts import local_forwarder


class EchoServer:
    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            connection, _ = self.socket.accept()
            data = connection.recv(1024)
            connection.sendall(b'echo:' + data)
            connection.close()
        except OSError:
            pass

    def close(self):
        self.socket.close()


class ForwarderTests(unittest.TestCase):
    def test_pin_to_loopback(self):
        sock = socket.socket()
        try:
            local_forwarder.pin(sock, 'lo')
        finally:
            sock.close()

    def test_forward_through_pinned_interface(self):
        echo = EchoServer()
        port_socket = socket.socket(); port_socket.bind(('127.0.0.1', 0))
        listen_port = port_socket.getsockname()[1]; port_socket.close()
        thread = threading.Thread(target=local_forwarder.forward,
                                  args=(listen_port, '127.0.0.1', echo.port, ['lo']),
                                  kwargs={'require_nats': False}, daemon=True)
        thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', listen_port), timeout=1) as client:
                    client.sendall(b'hello')
                    reply = client.recv(1024)
                    self.assertEqual(reply, b'echo:hello')
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail('forwarder did not accept a connection')
        echo.close()


if __name__ == '__main__':
    unittest.main()
