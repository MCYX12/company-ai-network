"""Isolated-stack lifecycle guards: production refusal and PID ownership facts."""
import os
import tempfile
import unittest
from pathlib import Path

from scripts import iso_stack


class IsoStackGuardTests(unittest.TestCase):
    def test_refuses_production_runtimes(self):
        with self.assertRaises(SystemExit):
            iso_stack._refuse_production(iso_stack.ROOT/'.runtime-lan')
        with self.assertRaises(SystemExit):
            iso_stack._refuse_production(iso_stack.ROOT/'.runtime')
        with tempfile.TemporaryDirectory() as folder:
            iso_stack._refuse_production(Path(folder))   # a temp runtime is allowed

    def test_proc_info_reads_start_time_and_cmdline(self):
        info=iso_stack._proc_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertTrue(info.get('starttime'))
        self.assertTrue(info.get('cmdline'))

    def test_proc_info_of_a_missing_pid_is_none(self):
        self.assertIsNone(iso_stack._proc_info(999999999))


if __name__=='__main__':
    unittest.main()
