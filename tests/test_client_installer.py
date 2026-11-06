import importlib.util,json,ssl,subprocess,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from scripts.build_client_installer import build,source_assets
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('installer',ROOT/'packaging/client/install.py');installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
class InstallerTests(unittest.TestCase):
    def test_source_allowlist_rejects_unexpected_files_and_linked_assets(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);shutil.copytree(ROOT/'packaging/client',root/'packaging/client')
            extra=root/'packaging/client'/'accidental.txt';extra.write_text('synthetic')
            with self.assertRaises(ValueError):source_assets(root)
            extra.unlink();self.assertTrue(source_assets(root))
            asset=root/'packaging/client'/'README.txt';asset.unlink();asset.symlink_to(ROOT/'packaging/client'/'README.txt')
            with self.assertRaises(ValueError):source_assets(root)
    def test_public_package_has_no_server_or_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'client.zip';build(archive)
            with zipfile.ZipFile(archive) as z:
                names=z.namelist()
                self.assertFalse(any(n.endswith(('.key','.crt','.sqlite3')) or 'access_admin' in n or '/router.py' in n for n in names))
                self.assertTrue(any(n.endswith('nats/aio/client.py') for n in names))
                self.assertIn('company-agent-client/install_windows.ps1', names)
                self.assertIn('company-agent-client/selfcheck.ps1', names)
                self.assertIn('company-agent-client/ollama_adapter.py', names)
                self.assertIn('company-agent-client/dsh_desktop_adapter.py', names)
                self.assertIn('company-agent-client/dsh_desktop_profile.cjs', names)
    def test_installed_local_model_adapter_imports_without_external_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);archive=root/'client.zip';build(archive)
            with zipfile.ZipFile(archive) as z:z.extractall(root/'source')
            # An unpacked package can contain local leftovers: only verified members ship.
            (root/'source/company-agent-client/app/unverified.py').write_text('raise RuntimeError("injected")')
            dest=root/'installed'
            config={'version':1,'identity':'alice','endpoint':'tls://localhost:14222'}
            files={f'certs/{name}':b'fixture' for name in ('ca.crt','alice.crt','alice.key')}
            with patch.object(installer,'credentials',return_value=(config,files)),patch.object(installer.ssl,'create_default_context'):
                installer.install(root/'source/company-agent-client',root/'unused.zip',dest,[])
            self.assertFalse((dest/'app/unverified.py').exists())
            self.assertTrue((dest/'selfcheck.ps1').is_file())
            self.assertEqual(json.loads((dest/'installed-manifest.json').read_text())['files']['worker.py'],
                             __import__('hashlib').sha256((dest/'worker.py').read_bytes()).hexdigest())
            result=subprocess.run([sys.executable,'-S','-B','-c','import ollama_adapter,dsh_desktop_adapter;from pathlib import Path;assert Path("dsh_desktop_profile.cjs").is_file();assert ollama_adapter.BASE=="http://127.0.0.1:11434"'],cwd=dest,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
    def test_windows_skill_paths_are_powershell_quoted(self):
        with patch.object(installer.sys, 'platform', 'win32'):
            self.assertEqual(installer.skill_command("C:\\Program Files\\Fei's Python\\python.exe", True),
                             "& 'C:\\Program Files\\Fei''s Python\\python.exe'")
            text=installer.chat_skill(ROOT/'packaging/client',Path('client with spaces'))
            self.assertIn('$env:PYTHONUTF8',text)
            self.assertIn('Get-Content -LiteralPath',text)
            self.assertNotIn('--wait < <问题文件>',text)
    def test_reject_extra_credentials_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'identity.zip'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('enrollment.json',json.dumps(dict(version=1,identity='alice',endpoint='tls://localhost:14222')))
                for n in ['certs/ca.crt','certs/alice.crt','../alice.key']:z.writestr(n,'x')
            with self.assertRaises(ValueError):installer.credentials(p)
    def test_install_rollback_and_existing_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);dest=root/'destination';dest.mkdir();(dest/'keep').write_text('ok')
            with patch.object(installer,'credentials',return_value=({},{})):
                with self.assertRaises(ValueError):installer.install(root,root/'x',dest,[])
            self.assertEqual((dest/'keep').read_text(),'ok')
