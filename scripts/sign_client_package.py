"""Sign an offline client ZIP with an operator-provided RSA publisher key."""
import argparse,base64,importlib.util,json,os
from pathlib import Path
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa,padding,utils

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('package_trust',ROOT/'packaging/client/package_trust.py')
trust=importlib.util.module_from_spec(spec);spec.loader.exec_module(trust)

def public_record(key):
    public=key.public_key().public_numbers()
    if key.key_size not in (3072,4096) or public.e!=65537:raise ValueError('RSA 3072/4096 publisher key required')
    return {'schema_version':1,'algorithm':'rsa-pkcs1v15-sha256','n':format(public.n,'x'),'e':public.e}

def sign(package,private_key,output):
    key=serialization.load_pem_private_key(Path(private_key).read_bytes(),password=None)
    if not isinstance(key,rsa.RSAPrivateKey):raise ValueError('RSA publisher key required')
    public=public_record(key);sha=trust.digest(package)
    signature=key.sign(bytes.fromhex(sha),padding.PKCS1v15(),utils.Prehashed(hashes.SHA256()))
    record={'schema_version':1,'algorithm':public['algorithm'],'key_id':trust.key_id(public),
            'sha256':sha,'signature':base64.b64encode(signature).decode()}
    output=Path(output)
    with output.open('x',encoding='utf-8') as stream:stream.write(json.dumps(record,sort_keys=True,indent=2)+'\n')
    return public

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--package',type=Path)
    p.add_argument('--private-key',type=Path,required=True);p.add_argument('--signature',type=Path)
    p.add_argument('--public-key-out',type=Path);p.add_argument('--generate-development-key',action='store_true')
    a=p.parse_args()
    if a.generate_development_key:
        if a.package or a.signature or not a.public_key_out:p.error('Generate requires --public-key-out only')
        key=rsa.generate_private_key(public_exponent=65537,key_size=3072)
        a.private_key.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(a.private_key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        public=public_record(key)
    else:
        if not a.package or not a.signature:p.error('Sign requires --package and --signature')
        public=sign(a.package,a.private_key,a.signature)
    if a.public_key_out:
        with a.public_key_out.open('x',encoding='utf-8') as stream:stream.write(json.dumps(public,sort_keys=True,indent=2)+'\n')
    print(json.dumps({'key_id':trust.key_id(public),'development_key_generated':a.generate_development_key}))
if __name__=='__main__':main()
