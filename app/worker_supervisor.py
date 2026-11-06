"""Bounded worker output and one launcher per installation, including Windows SCM."""
import os
import subprocess
import sys
from pathlib import Path


class BoundedLog:
    def __init__(self, path, max_bytes=10 * 1024 * 1024, backups=5):
        if type(max_bytes) is not int or max_bytes < 1024 or type(backups) is not int or not 1 <= backups <= 20:
            raise ValueError('Invalid log retention')
        self.path = Path(path)
        self.max_bytes, self.backups = max_bytes, backups
        self.stream = None

    def rotate(self):
        from .client_material import independent
        for candidate in [self.path] + [self.path.with_name(self.path.name + '.' + str(i))
                                        for i in range(1, self.backups + 1)]:
            if candidate.exists() or candidate.is_symlink():
                independent(candidate)
        if self.stream:
            self.stream.close()
            self.stream = None
        oldest = self.path.with_name(self.path.name + '.' + str(self.backups))
        if oldest.exists():
            oldest.unlink()
        for i in range(self.backups - 1, 0, -1):
            source = self.path.with_name(self.path.name + '.' + str(i))
            if source.exists():
                source.replace(self.path.with_name(self.path.name + '.' + str(i + 1)))
        if self.path.exists():
            self.path.replace(self.path.with_name(self.path.name + '.1'))

    def write(self, data):
        view = memoryview(data)
        while view:
            if self.stream is None:
                # Never follow a replaced link into an unrelated file.
                from .client_material import independent
                if self.path.exists() or self.path.is_symlink():
                    independent(self.path)
                self.stream = self.path.open('ab', buffering=0)
            size = self.stream.tell()
            if size >= self.max_bytes:
                self.rotate()
                continue
            count = min(len(view), self.max_bytes - size)
            self.stream.write(view[:count])
            view = view[count:]

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def launch(root):
    root = Path(root).resolve()
    runtime = root / '.runtime'
    runtime.mkdir(exist_ok=True)
    from .client_material import independent
    independent(root, True)
    independent(runtime, True)
    lock_path = runtime / 'launcher.lock'
    if lock_path.exists() or lock_path.is_symlink():
        independent(lock_path)
    with lock_path.open('a+b') as lock:
        # Windows byte-range locks also deny reads of the locked byte. Inspect
        # file length without reading it, then attempt locking before any I/O.
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 73  # duplicate scheduler/service launch, never run two identities
        log = BoundedLog(root / 'worker.log')
        from .worker_kernel import process_options, terminate_process_tree
        try:
            with subprocess.Popen([sys.executable, '-u', str(root / 'worker.py'), '--root', str(root)],
                                  cwd=root, env=dict(os.environ, PYTHONUTF8='1'),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  **process_options()) as proc:
                try:
                    while True:
                        chunk = proc.stdout.read1(65536)
                        if not chunk:
                            break
                        log.write(chunk)
                    return proc.wait()
                finally:
                    if proc.poll() is None:
                        terminate_process_tree(proc)
        finally:
            log.close()
