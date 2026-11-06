"""Export one existing enabled client identity; does not approve conversations."""
import argparse,json,os,re,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.registry import validate_registry

def export(runtime,identity,output,default_target=None):
    from app.common import authority_base
    rows=validate_registry(json.loads((Path(authority_base(runtime))/'registry.json').read_text()))
    if identity not in rows or not rows[identity]['enabled']:raise ValueError('Unknown or disabled client identity')
    if default_target is not None and (not isinstance(default_target,str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',default_target)):
        raise ValueError('Invalid default target')
    if output.exists():raise ValueError('Output exists; preserved')
    names=['ca.crt',identity+'.crt',identity+'.key']
    for name in names:
        p=runtime/'certs'/name
        if p.is_symlink() or not p.is_file():raise ValueError('Regular credential files required')
    os.umask(0o077);output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',zipfile.ZIP_DEFLATED) as z:
        metadata=dict(version=1,identity=identity,endpoint=(runtime/'endpoint.txt').read_text().strip())
        if default_target:metadata['default_target']=default_target
        z.writestr('enrollment.json',json.dumps(metadata))
        for name in names:z.write(runtime/'certs'/name,'certs/'+name)
    output.chmod(0o600)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',required=True,type=Path);p.add_argument('--identity',required=True);p.add_argument('--output',required=True,type=Path);p.add_argument('--default-target',help='Optional agent id the installed client prefers when --target is omitted');a=p.parse_args();export(a.runtime,a.identity,a.output,a.default_target)
