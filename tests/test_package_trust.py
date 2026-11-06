import importlib.util,json,tempfile,unittest
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from scripts.sign_client_package import sign,public_record,trust

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.package=self.root/'client.zip';self.package.write_bytes(b'PACKAGE FIXTURE')
        self.private=self.root/'signer';self.private.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.public=self.root/'publisher.json';self.public.write_text(json.dumps(public_record(self.key)))
        self.signature=self.root/'signature.json';sign(self.package,self.private,self.signature)
    def tearDown(self):self.tmp.cleanup()
    def test_signature_matches_cryptography_and_rejects_changed_package(self):
        self.assertTrue(trust.verify(self.package,self.signature,self.public)['publisher_verified'])
        self.package.write_bytes(b'CHANGED')
        with self.assertRaises(ValueError):trust.verify(self.package,self.signature,self.public)
    def test_modified_signature_and_wrong_publisher_rejected(self):
        record=json.loads(self.signature.read_text());record['signature']='A'*512
        self.signature.write_text(json.dumps(record))
        with self.assertRaises(ValueError):trust.verify(self.package,self.signature,self.public)
        self.signature.unlink();sign(self.package,self.private,self.signature)
        key=json.loads(self.public.read_text());key['n']=format(int(key['n'],16)^2,'x');self.public.write_text(json.dumps(key))
        with self.assertRaises(ValueError):trust.verify(self.package,self.signature,self.public)
    def test_boolean_version_or_extra_claim_not_accepted(self):
        record=json.loads(self.signature.read_text())
        for mutated in ({**record,'schema_version':True},{**record,'public_key':'self-provided'}):
            self.signature.write_text(json.dumps(mutated))
            with self.assertRaises(ValueError):trust.verify(self.package,self.signature,self.public)
    def test_public_verifier_runs_without_site_packages(self):
        import subprocess,sys
        p=subprocess.run([sys.executable,'-S',str(Path(trust.__file__)), '--package',str(self.package),'--signature',str(self.signature),'--trusted-key',str(self.public)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr);self.assertTrue(json.loads(p.stdout)['publisher_verified'])
