"""Offline RSA publisher verification. Provision the public key outside the ZIP.

Only verification uses this standard-library module; private keys stay on the publisher.
The signing implementation lives in scripts/sign_client_package.py.
"""
import argparse, base64, hashlib, json, re
from pathlib import Path

PREFIX=bytes.fromhex('3031300d060960864801650304020105000420')
def load_key(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>4096:raise ValueError('Invalid publisher trust')
    key=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(key,dict) or set(key)!={'schema_version','algorithm','n','e'}
        or type(key['schema_version']) is not int or key['schema_version']!=1
        or key['algorithm']!='rsa-pkcs1v15-sha256' or key['e']!=65537 or type(key['e']) is not int
        or not isinstance(key['n'],str) or not re.fullmatch('[1-9a-f][0-9a-f]{767,1023}',key['n'])):
        raise ValueError('Invalid publisher trust')
    n=int(key['n'],16)
    if n.bit_length() not in (3072,4096) or n%2!=1:raise ValueError('Invalid RSA modulus')
    return key,n

def key_id(key):
    return hashlib.sha256(json.dumps(key,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def verify(package,signature,trusted_key,package_data=None):
    key,n=load_key(trusted_key);path=Path(signature)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>4096:raise ValueError('Invalid package signature')
    record=json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(record,dict) or set(record)!={'schema_version','algorithm','key_id','sha256','signature'}
        or type(record['schema_version']) is not int or record['schema_version']!=1
        or record['algorithm']!=key['algorithm'] or record['key_id']!=key_id(key)
        or record['sha256']!=(hashlib.sha256(package_data).hexdigest() if package_data is not None else digest(package)) or not isinstance(record['signature'],str)):
        raise ValueError('Package publisher verification failed')
    try:raw=base64.b64decode(record['signature'],validate=True)
    except (ValueError,TypeError):raise ValueError('Invalid package signature') from None
    size=(n.bit_length()+7)//8
    if len(raw)!=size or int.from_bytes(raw,'big')>=n:raise ValueError('Invalid package signature')
    encoded=pow(int.from_bytes(raw,'big'),key['e'],n).to_bytes(size,'big')
    tail=PREFIX+bytes.fromhex(record['sha256'])
    expected=b'\x00\x01'+b'\xff'*(size-len(tail)-3)+b'\x00'+tail
    if encoded!=expected:raise ValueError('Package publisher verification failed')
    return {'publisher_verified':True,'key_id':key_id(key),'package_sha256':record['sha256']}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--package',type=Path,required=True);p.add_argument('--signature',type=Path,required=True)
    p.add_argument('--trusted-key',type=Path,required=True);a=p.parse_args()
    try:print(json.dumps(verify(a.package,a.signature,a.trusted_key)))
    except (OSError,ValueError,TypeError,KeyError):raise SystemExit('Package publisher verification failed')
