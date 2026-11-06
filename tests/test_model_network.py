import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app.model_network import model_environment
class NetworkTests(unittest.TestCase):
    def test_proxy_only_affects_codex_child(self):
        with tempfile.TemporaryDirectory() as d,patch('app.model_network.RUNTIME',Path(d)),patch.dict(os.environ,{'HTTPS_PROXY':'https://existing.example'}):
            p=Path(d)/'model-network.json';p.write_text(json.dumps({'codex_proxy':'http://127.0.0.1:7897'}));p.chmod(0o600)
            self.assertEqual(model_environment('codex')['HTTPS_PROXY'],'http://127.0.0.1:7897')
            self.assertEqual(model_environment('claude-code')['HTTPS_PROXY'],'https://existing.example')
            self.assertEqual(os.environ['HTTPS_PROXY'],'https://existing.example')
    def test_remote_and_credential_proxy_rejected(self):
        with tempfile.TemporaryDirectory() as d,patch('app.model_network.RUNTIME',Path(d)):
            for url in ['http://example.com:7897','http://user:password@127.0.0.1:7897','http://127.0.0.1:7897/private']:
                p=Path(d)/'model-network.json';p.write_text(json.dumps({'codex_proxy':url}));p.chmod(0o600)
                with self.assertRaises(ValueError):model_environment('codex')
    def test_missing_config_preserves_environment(self):
        with tempfile.TemporaryDirectory() as d,patch('app.model_network.RUNTIME',Path(d)):
            self.assertEqual(model_environment('codex'),dict(os.environ))
if __name__=='__main__':unittest.main()
