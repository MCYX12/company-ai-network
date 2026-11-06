"""Offline, per-user client installer. Requires Python >=3.11."""
import argparse,hashlib,json,os,re,shutil,ssl,sys,tempfile,zipfile,shlex
from pathlib import Path
from urllib.parse import urlsplit

def user_base():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData'/'Local'))
    return Path.home()/('Library/Application Support' if sys.platform=='darwin' else '.local/share')

def skill_command(path, executable=False):
    if sys.platform == 'win32':
        quoted="'"+str(path).replace("'", "''")+"'"
        return ('& ' if executable else '')+quoted
    return shlex.quote(str(path))

def chat_skill(source, dest):
    command=skill_command(sys.executable,True)+' '+skill_command(dest/'client.py')
    text=(source/'SKILL.md').read_text(encoding='utf-8').replace('@PYTHON@',skill_command(sys.executable,True)).replace('@CLIENT@',skill_command(dest/'client.py'))
    if sys.platform == 'win32':
        stdin="$env:PYTHONUTF8='1'; $OutputEncoding = [System.Text.UTF8Encoding]::new($false); Get-Content -LiteralPath '<问题文件>' -Raw -Encoding UTF8 | "+command+' --target <目标ID> --question-stdin --wait'
        text=text.replace('--target <目标ID> --question-stdin --wait < <问题文件>',stdin)
    return text

def credentials(path):
    with zipfile.ZipFile(path) as z:
        if len(z.infolist())!=4 or any(i.file_size>65536 for i in z.infolist()):raise ValueError('Invalid enrollment package size')
        names=z.namelist()
        if 'enrollment.json' not in names:raise ValueError('Missing enrollment.json')
        config=json.loads(z.read('enrollment.json'))
        required={'version','identity','endpoint'}
        if not required<=set(config)<=required|{'default_target'} or config['version']!=1:raise ValueError('Invalid enrollment metadata')
        ident=config['identity']
        if not isinstance(ident,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',ident) or ident in {'router','server','unregistered'}:raise ValueError('Invalid client identity')
        default_target=config.get('default_target')
        if default_target is not None and (not isinstance(default_target,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',default_target) or default_target in {'router','server','unregistered'}):raise ValueError('Invalid default target')
        url=urlsplit(config['endpoint'])
        if url.scheme!='tls' or not url.hostname or not url.port or url.username or url.password or url.path or url.query or url.fragment:raise ValueError('Explicit TLS endpoint required')
        expected={'enrollment.json','certs/ca.crt',f'certs/{ident}.crt',f'certs/{ident}.key'}
        if set(names)!=expected:raise ValueError('Unexpected files in enrollment package')
        return config,{name:z.read(name) for name in expected if name!='enrollment.json'}

def install(source,enrollment,dest,skill_dirs,publisher=None):
    from app.client_material import read_bytes,safe_name
    if sys.version_info<(3,11) or sys.platform not in {'linux','darwin','win32'}:raise ValueError('Requires Python 3.11+ on macOS, Linux or Windows')
    config,files=credentials(enrollment)
    if dest.exists() or dest.is_symlink():raise ValueError('Installation already exists; preserved. Choose a new --destination')
    if any(p.exists() or p.is_symlink() for p in skill_dirs):raise ValueError('Skill already exists; preserved')
    manifest=json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if publisher is not None:
        import io,package_trust
        package,signature,key=publisher
        package_raw,_=read_bytes(Path(package).absolute().parent,Path(package).name,8*1024*1024)
        package_trust.verify(package,signature,key,package_raw)
        with zipfile.ZipFile(io.BytesIO(package_raw)) as archive:
            signed_manifest=json.loads(archive.read('company-agent-client/manifest.json'))
            if manifest!=signed_manifest:raise ValueError('Extracted source differs from signed package')
    if not isinstance(manifest,dict) or not 1<=len(manifest)<=200:raise ValueError('Invalid package inventory')
    verified={}
    for name,digest in manifest.items():
        safe_name(name);raw,_=read_bytes(source,name,1024*1024)
        if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Package integrity check failed')
        verified[name]=raw
    os.umask(0o077);dest.parent.mkdir(parents=True,exist_ok=True)
    # Stage validation before publishing an installation.
    stage=Path(tempfile.mkdtemp(prefix='.company-install-',dir=dest.parent));created=[]
    try:
        top={'worker.py','ollama_adapter.py','dsh_desktop_adapter.py','dsh_desktop_profile.cjs','dsh_desktop_status.cjs','history_export.py','upgrade.py','worker_doctor.py','THIRD_PARTY_LICENSE.txt','selfcheck.ps1','install_windows.ps1','install.py','README.txt','SKILL.md','package_trust.py','configure_windows_worker.ps1'}
        installed={}
        for name,raw in verified.items():
            if name in top or name.startswith(('app/','nats/')) and name.endswith('.py'):
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw);installed[name]=manifest[name]
        (stage/'installed-manifest.json').write_text(json.dumps({'schema_version':1,'package_sha256':None,'files':installed},sort_keys=True,indent=2))
        runtime=stage/'.runtime';(runtime/'certs').mkdir(parents=True)
        for name,data in files.items():(runtime/name).write_bytes(data)
        ident=config['identity'];ctx=ssl.create_default_context(cafile=str(runtime/'certs/ca.crt'));ctx.load_cert_chain(str(runtime/f'certs/{ident}.crt'),str(runtime/f'certs/{ident}.key'))
        (stage/'client.json').write_text(json.dumps(config))
        launcher='''import json,os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
config=json.loads((root/'client.json').read_text(encoding='utf-8'))
os.environ['POC_RUNTIME']=str(root/'.runtime')
os.environ['NATS_URL']=config['endpoint']
os.environ['AGENT_IDENTITY']=config['identity']
os.environ['AGENT_DEFAULT_TARGET']=config.get('default_target','')
if '--identity' in sys.argv:raise SystemExit('Use the installed certificate identity')
from app.peer_client import main
main()
'''
        (stage/'client.py').write_text(launcher)
        stage.rename(dest);created.append(dest)
        for skill in skill_dirs:
            skill.mkdir(parents=True);created.append(skill)
            text=chat_skill(source,dest)
            (skill/'SKILL.md').write_text(text,encoding='utf-8')
        print(json.dumps({'installed':str(dest),'identity':ident,'skills':[str(p) for p in skill_dirs]},ensure_ascii=False))
    except BaseException:
        for p in reversed(created):shutil.rmtree(p)
        raise
    finally:
        if stage.exists():shutil.rmtree(stage)

def install_registration(source,skills):
    base=user_base()
    target=base/'CompanyAgentRegistration'
    roots=[]
    if skills in {'codex','both'}:roots.append(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'skills/company-agent-register')
    if skills in {'claude','both'}:roots.append(Path.home()/'.claude/skills/company-agent-register')
    if target.exists() or any(p.exists() for p in roots):raise ValueError('Registration tool or skill exists; preserved')
    os.umask(0o077);target.mkdir(parents=True)
    shutil.copy2(source/'register.py',target/'register.py')
    text=(source/'REGISTER_SKILL.md').read_text(encoding='utf-8').replace('@PYTHON@',skill_command(sys.executable,True)).replace('@REGISTER@',skill_command(target/'register.py'))
    for folder in roots:
        folder.mkdir(parents=True);(folder/'SKILL.md').write_text(text,encoding='utf-8')
    print('成员注册技能已安装。新会话中说：使用 company-agent-register 注册成员。')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--registration-only',action='store_true');p.add_argument('--enrollment',type=Path);p.add_argument('--destination',type=Path);p.add_argument('--skills',choices=['codex','claude','both','none'],default='none')
    p.add_argument('--package',type=Path);p.add_argument('--signature',type=Path);p.add_argument('--trusted-key',type=Path);a=p.parse_args()
    if a.registration_only:
        install_registration(Path(__file__).resolve().parent,a.skills);return
    if not all((a.package,a.signature,a.trusted_key)):p.error('Installation requires --package, --signature and independently provisioned --trusted-key')
    if not a.enrollment:
        a.enrollment=Path(input('个人接入包路径（管理员提供的 .zip）：').strip().strip('"').strip("'" )).expanduser()
        a.skills=input('安装技能到 codex / claude / both / none [codex]：').strip() or 'codex'
        if a.skills not in {'codex','claude','both','none'}:p.error('Invalid skill choice')
    base=user_base()
    dest=a.destination or base/'CompanyAgentClient'
    skills=[]
    if a.skills in {'codex','both'}:skills.append(Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'skills/company-agent-chat')
    if a.skills in {'claude','both'}:skills.append(Path.home()/'.claude/skills/company-agent-chat')
    install(Path(__file__).resolve().parent,a.enrollment,dest,skills,(a.package,a.signature,a.trusted_key))
if __name__=='__main__':
    try:main()
    except (ValueError,OSError,ssl.SSLError,zipfile.BadZipFile) as e:raise SystemExit(str(e))
