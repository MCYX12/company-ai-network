import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from app.worker_supervisor import BoundedLog

ROOT=Path(__file__).resolve().parents[1]

class Tests(unittest.TestCase):
    def test_long_running_output_has_bounded_tail_and_rotates_without_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'worker.log';log=BoundedLog(path,1024,3)
            data=bytes(range(256))*50
            for i in range(0,len(data),37):log.write(data[i:i+37])
            log.close()
            files=[path.with_name('worker.log.'+str(i)) for i in (3,2,1)]+[path]
            self.assertTrue(all(p.stat().st_size<=1024 for p in files))
            retained=b''.join(p.read_bytes() for p in files)
            self.assertTrue(data.endswith(retained));self.assertLessEqual(len(retained),4096)

    def test_linked_backups_are_not_removed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);secret=root/'unrelated';secret.write_bytes(b'KEEP')
            try:os.link(secret,root/'worker.log.1')
            except OSError:self.skipTest('Hard links unavailable')
            log=BoundedLog(root/'worker.log',1024,2)
            try:
                with self.assertRaises(ValueError):log.write(b'x'*2048)
            finally:log.close()
            self.assertEqual(secret.read_bytes(),b'KEEP')

    def test_duplicate_launcher_rejected_and_nonzero_child_propagated(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'worker.py').write_text("import time,sys\nprint('START',flush=True)\ntime.sleep(2)\nsys.exit(17)\n")
            code='from app.worker_supervisor import launch;import sys;sys.exit(launch(sys.argv[1]))'
            with subprocess.Popen([sys.executable,'-c',code,str(root)],cwd=ROOT) as first:
                deadline=time.monotonic()+5
                while time.monotonic()<deadline and not (root/'worker.log').exists():time.sleep(.02)
                second=subprocess.run([sys.executable,'-c',code,str(root)],cwd=ROOT,timeout=5)
                self.assertEqual(second.returncode,73)
                self.assertEqual(first.wait(timeout=5),17)
            self.assertIn(b'START',(root/'worker.log').read_bytes())
            # OS releases the lock when the first process closes; stale lock file is safe.
            self.assertEqual(subprocess.run([sys.executable,'-c',code,str(root)],cwd=ROOT,timeout=5).returncode,17)

if __name__=='__main__':unittest.main()
