"""POSIX snapshot-based local file grants. No history, shell, downloads or whole-disk mode."""
import hashlib,json,os,re,sqlite3,stat,time,uuid
from pathlib import Path
from .common import authority_base, RUNTIME
ID=re.compile(r'^[a-z][a-z0-9-]{0,63}$')
EXT={'.md','.txt','.rst'}
SECRET=re.compile(r'-----BEGIN .*PRIVATE KEY-----|(?i:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+')

def read_safe(path):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise ValueError('Absolute path required')
    if any(p.startswith('.') for p in path.parts[1:]) or path.suffix.lower() not in EXT:raise ValueError('File type or hidden path excluded')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        child=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            before=os.fstat(child)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_size>262144:raise ValueError('Regular single-link file up to 256 KiB required')
            data=b''
            while len(data)<=262144:
                chunk=os.read(child,min(65536,262145-len(data)))
                if not chunk:break
                data+=chunk
            after=os.fstat(child)
            if len(data)>262144 or (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('File changed while reading')
            text=data.decode('utf-8')
            if '\x00' in text or SECRET.search(text):raise ValueError('Sensitive or binary content excluded')
            return text,dict(device=before.st_dev,inode=before.st_ino,sha256=hashlib.sha256(data).hexdigest(),size=len(data))
        finally:os.close(child)
    finally:os.close(fd)

class FileAccess:
    def __init__(self,path=None):
        self.path=path or authority_base(RUNTIME)/'file-access.sqlite3'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS scopes(id TEXT PRIMARY KEY, caller TEXT,target TEXT,project TEXT,expires REAL,revoked INTEGER,cloud INTEGER,manifest TEXT,used INTEGER DEFAULT 0,calls INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS file_audit(scope TEXT,event TEXT,time REAL,bytes INTEGER)')
    def db(self):return sqlite3.connect(self.path,timeout=5)
    def create(self,caller,target,project,path,kind='file',depth=0,seconds=3600,cloud=False):
        if any(not isinstance(x,str) or not ID.fullmatch(x) for x in [caller,target,project]):raise ValueError('Invalid identity/project')
        if type(seconds)!=int or not 1<=seconds<=86400 or type(depth)!=int or not 0<=depth<=4 or type(cloud)!=bool:raise ValueError('Invalid limits')
        base=Path(path)
        if not base.is_absolute() or '..' in base.parts or any(x.startswith('.') for x in base.parts[1:]):raise ValueError('Absolute non-hidden path required')
        # Reject symlink ancestors before enumeration. Files are re-opened with no-follow descriptors.
        if any(p.is_symlink() for p in [base,*base.parents]):raise ValueError('Symlink excluded')
        paths=[];start=time.monotonic();count=0
        if kind=='file':paths=[base]
        elif kind=='directory' and base.is_dir():
            if base==Path('/') or len(base.parts)<3:raise ValueError('Choose a specific project directory')
            for directory,dirs,files in os.walk(base,followlinks=False):
                count+=len(dirs)+len(files)
                if count>2000 or time.monotonic()-start>10:raise ValueError('Discovery budget exceeded; narrow scope')
                level=len(Path(directory).relative_to(base).parts)
                dirs[:]=sorted(d for d in dirs if not d.startswith('.') and not (Path(directory)/d).is_symlink()) if level<depth else []
                paths.extend(Path(directory)/f for f in sorted(files) if not f.startswith('.') and Path(f).suffix.lower() in EXT)
        else:raise ValueError('Only file or directory supported')
        if not 1<=len(paths)<=20:raise ValueError('Select 1..20 text files')
        manifest=[];total=0
        for p in paths:
            _,fingerprint=read_safe(p);total+=fingerprint['size'];manifest.append(dict(path=str(p),**fingerprint))
        if total>2097152:raise ValueError('2 MiB scope limit')
        ident=str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO scopes(id,caller,target,project,expires,revoked,cloud,manifest) VALUES(?,?,?,?,?,0,?,?)',(ident,caller,target,project,time.time()+seconds,int(cloud),json.dumps(manifest)))
            db.execute('INSERT INTO file_audit VALUES(?,?,?,?)',(ident,'created',time.time(),0))
        return ident
    def listing(self):
        with self.db() as db:
            db.row_factory=sqlite3.Row;rows=db.execute('SELECT * FROM scopes ORDER BY rowid DESC LIMIT 100').fetchall()
        return [dict(r,manifest=json.loads(r['manifest'])) for r in rows]
    def revoke(self,ident):
        with self.db() as db:
            if db.execute('UPDATE scopes SET revoked=1 WHERE id=?',(ident,)).rowcount!=1:raise ValueError('Unknown scope')
            db.execute('INSERT INTO file_audit VALUES(?,?,?,?)',(ident,'revoked',time.time(),0))
    def read(self,ident,caller,target,project,for_cloud=False):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');db.row_factory=sqlite3.Row
            r=db.execute('SELECT * FROM scopes WHERE id=?',(ident,)).fetchone()
            if not r or r['revoked'] or r['expires']<=time.time() or (r['caller'],r['target'],r['project'])!=(caller,target,project) or (for_cloud and not r['cloud']):raise ValueError('File scope denied')
            manifest=json.loads(r['manifest']);size=sum(f['size'] for f in manifest)
            if r['calls']>=100 or r['used']+size>20971520:raise ValueError('Scope budget exhausted')
            db.execute('UPDATE scopes SET used=used+?,calls=calls+1 WHERE id=?',(size,ident))
        out=[]
        for f in manifest:
            self.check(ident)
            text,fp=read_safe(f['path'])
            if any(fp[k]!=f[k] for k in fp):raise ValueError('File changed: reapproval required')
            out.append(dict(name=Path(f['path']).name,text=text,sha256=fp['sha256']))
        self.check(ident)
        with self.db() as db:db.execute('INSERT INTO file_audit VALUES(?,?,?,?)',(ident,'read_cloud' if for_cloud else 'read_local',time.time(),size))
        return out
    def check(self,ident):
        with self.db() as db:r=db.execute('SELECT revoked,expires FROM scopes WHERE id=?',(ident,)).fetchone()
        if not r or r[0] or r[1]<=time.time():raise ValueError('Scope revoked or expired')
