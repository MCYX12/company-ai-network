"""Reject runtime artifacts and private key material in the Git upload set.

Ported from the public release candidate. Run before publishing any subset of this repo
(e.g. ``python -m scripts.check_public``); it inspects ``git ls-files`` only, so ignored
runtime directories are not reported.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def tracked(root=ROOT):
    """Repo-relative paths under version control: the set that would be published."""
    out=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode()
    return [p for p in out.split('\0') if p]

def problems(paths,root=ROOT):
    errors=[]
    for name in paths:
        p=Path(name)
        if (any(part.startswith(('.runtime','.venv')) or part in {'exports','dist-private','backups'} for part in p.parts)
            or p.suffix in {'.key','.crt','.csr','.sqlite3','.db'} or p.name=='.env'
            or p.name.startswith('nats-server') and p.suffix!='.md'):
            errors.append(name+': runtime or machine-specific artifact')
        data=(root/p).read_bytes()
        if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data):
            errors.append(name+': private key material')
    return errors

def main():
    errors=problems(tracked())
    for error in errors:print(error)
    print('Public file checks: '+('FAILED' if errors else 'OK'))
    return bool(errors)

if __name__=='__main__':sys.exit(main())
