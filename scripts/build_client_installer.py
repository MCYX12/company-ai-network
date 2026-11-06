"""Build a public offline client ZIP with a strict source allowlist."""
import hashlib,importlib.metadata,json,shutil,subprocess,sys,tempfile,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
# Every app module the client entry points import must be listed here. site.py is
# imported by common.py since the site-profile change; omitting it shipped a client
# that raised ModuleNotFoundError on every command (v25).
CLIENT_MODULES=['__init__.py','common.py','site.py','peer_client.py','client_doctor.py','worker_kernel.py','project_records.py','project_retirement.py','project_state.py','doc_index.py','client_material.py','client_history.py','native_work.py','history_access.py','history_progress.py','maintenance.py','worker_supervisor.py']
CLIENT_ASSETS={'README.txt','worker.py','dsh_desktop_adapter.py','worker_doctor.py','install.py',
               'history_export.py','dsh_desktop_status.cjs','register.py','upgrade.py','SKILL.md',
               'install.command','ollama_adapter.py','REGISTER_SKILL.md','dsh_desktop_profile.cjs',
               'package_trust.py','run-worker.py'}
def source_assets(root):
    directory=root/'packaging/client'
    found={p.name for p in directory.iterdir() if p.name!='__pycache__'}
    if found!=CLIENT_ASSETS:raise ValueError('Client asset allowlist mismatch')
    paths=[directory/name for name in sorted(CLIENT_ASSETS)]
    for p in paths:
        if p.is_symlink() or not p.is_file() or p.stat().st_nlink!=1:raise ValueError('Unsafe client asset')
    return paths
def build(output):
    dist=importlib.metadata.distribution('nats-py')
    if dist.version!='2.12.0':raise ValueError('Expected nats-py 2.12.0')
    with tempfile.TemporaryDirectory() as tmp:
        stage=Path(tmp)/'company-agent-client';stage.mkdir()
        for p in source_assets(ROOT):shutil.copy2(p,stage/p.name)
        for name in ('install_windows.ps1', 'selfcheck.ps1','configure_windows_worker.ps1','configure_windows_service.ps1','install_windows_msi.ps1','windows_recovery_check.ps1'):
            shutil.copy2(ROOT/'scripts'/name,stage/name)
        shutil.copy2(ROOT/'packaging/windows/WorkerService.cs',stage/'WorkerService.cs')
        (stage/'app').mkdir()
        for name in CLIENT_MODULES:shutil.copy2(ROOT/'app'/name,stage/'app'/name)
        for p in dist.files:
            if str(p).startswith('nats/') and str(p).endswith('.py'):
                target=stage/str(p);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dist.locate_file(p),target)
        license_file=next(p for p in dist.files if str(p).endswith('/licenses/LICENSE'))
        shutil.copy2(dist.locate_file(license_file),stage/'THIRD_PARTY_LICENSE.txt')
        # Import the staged client with external site-packages disabled, so a missing
        # module or a broken vendored dependency fails the build instead of the user.
        probe=subprocess.run([sys.executable,'-S','-B','-c','import app.peer_client, app.client_doctor, app.worker_kernel, app.client_material, app.client_history, app.native_work, ollama_adapter, dsh_desktop_adapter'],
                             cwd=stage,capture_output=True,text=True)
        if probe.returncode!=0:
            raise ValueError('Staged client does not import: '+(probe.stderr or probe.stdout).strip()[-500:])
        manifest={p.relative_to(stage).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2))
        output.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
            for p in sorted(stage.rglob('*')):
                if p.is_file():z.write(p,p.relative_to(stage.parent))
    output.with_suffix('.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest()+'  '+output.name+'\n')
    print(output)
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'dist/company-agent-client-v42.zip')
    build(parser.parse_args().output)
