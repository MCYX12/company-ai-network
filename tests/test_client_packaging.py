"""The installed client must ship every app module its worker imports.

The standalone worker used to be stdlib+nats only; it now imports app.worker_kernel. A staging
path that copies worker.py without that module breaks the device worker at startup, which is
exactly what the JetStream core-compat drill hit. This parses the worker's imports and refuses
any app module the installer allowlist does not ship.
"""
import ast
import unittest
from pathlib import Path

from scripts.build_client_installer import CLIENT_MODULES

ROOT = Path(__file__).resolve().parents[1]


def app_modules_imported(source):
    modules = set()
    for node in ast.walk(ast.parse(source)):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            if name.startswith('app.'):
                modules.add(name.split('.')[1] + '.py')
    return modules


class ClientWorkerPackagingTests(unittest.TestCase):
    def test_worker_only_imports_shipped_app_modules(self):
        source = (ROOT / 'packaging/client/worker.py').read_text(encoding='utf-8')
        missing = app_modules_imported(source) - set(CLIENT_MODULES)
        self.assertEqual(missing, set(), 'worker.py imports app modules the installer does not ship: %s' % missing)

    def test_worker_kernel_is_shipped(self):
        self.assertIn('worker_kernel.py', CLIENT_MODULES)

    def test_guard_detects_an_unshipped_import(self):
        self.assertEqual(app_modules_imported('from app.secret import x\n'), {'secret.py'})


if __name__ == '__main__':
    unittest.main()
