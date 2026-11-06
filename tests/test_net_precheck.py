import errno
import socket
import unittest
from unittest.mock import patch

from scripts.net_precheck import classify, probe, run_plan


class ClassifyTests(unittest.TestCase):
    def test_expected_open_and_blocked(self):
        self.assertEqual(classify('open', 'open'), 'PASS')
        self.assertEqual(classify('open', 'refused'), 'FAIL')
        for result in ['refused', 'timeout', 'unreachable', 'error']:
            self.assertEqual(classify('blocked', result), 'PASS')
        self.assertEqual(classify('blocked', 'open'), 'FAIL')

    def test_invalid_expectation(self):
        with self.assertRaises(ValueError):
            classify('maybe', 'open')


class ProbeTests(unittest.TestCase):
    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_open_connection(self):
        with patch('scripts.net_precheck.socket.create_connection', return_value=self.Context()):
            self.assertEqual(probe('host', 1), 'open')

    def test_failure_modes(self):
        for error, expected in [(socket.timeout(), 'timeout'), (ConnectionRefusedError(), 'refused'),
                                (OSError(errno.ENETUNREACH, 'no route'), 'unreachable'),
                                (OSError(errno.EACCES, 'denied'), 'error')]:
            with patch('scripts.net_precheck.socket.create_connection', side_effect=error):
                self.assertEqual(probe('host', 1), expected)

    def test_invalid_input(self):
        with self.assertRaises(ValueError):
            probe('', 1)
        with self.assertRaises(ValueError):
            probe('host', 0)


class RunPlanTests(unittest.TestCase):
    def test_report_and_summary(self):
        with patch('scripts.net_precheck.probe', side_effect=['open', 'timeout']):
            report = run_plan([{'host': 'a', 'port': 1, 'expect': 'open'},
                               {'host': 'b', 'port': 2, 'expect': 'blocked', 'note': 'vlan B'}])
        self.assertEqual(report['summary'], {'total': 2, 'pass': 2, 'fail': 0})
        self.assertEqual(report['probes'][0]['verdict'], 'PASS')
        self.assertEqual(report['probes'][1]['result'], 'timeout')
        self.assertIn('hostname', report['source'])

    def test_rejects_bad_plans(self):
        with self.assertRaises(ValueError):
            run_plan([])
        with self.assertRaises(ValueError):
            run_plan([{'host': 'a', 'port': 1}])
        with self.assertRaises(ValueError):
            run_plan([{'host': 'a', 'port': 1, 'expect': 'nope'}])


if __name__ == '__main__':
    unittest.main()
