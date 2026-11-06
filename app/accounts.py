"""Local admin login and offline invited-member enrollment. No default password."""
import hashlib,hmac,json,re,secrets,sqlite3,time
from pathlib import Path
from .common import authority_base, RUNTIME

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def password_hash(password,salt):
    if not isinstance(password,str) or not 12<=len(password)<=128:raise ValueError('密码需为 12–128 个字符')
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()

class Accounts:
    def __init__(self,path=None):
        self.path=Path(path) if path else authority_base(RUNTIME)/'accounts.sqlite3'
        with self.db() as d:
            d.executescript('''CREATE TABLE IF NOT EXISTS admins(name TEXT PRIMARY KEY,salt TEXT,hash TEXT,enabled INTEGER,mtls_identity TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,admin TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS invites(token TEXT PRIMARY KEY,role TEXT,expires REAL,projects TEXT,used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY,name TEXT,device TEXT,projects TEXT,state TEXT);
CREATE TABLE IF NOT EXISTS account_events(time REAL,actor TEXT,event TEXT,subject TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(time REAL);''')
            columns={row[1] for row in d.execute('PRAGMA table_info(admins)')}
            if 'mtls_identity' not in columns:
                d.execute('ALTER TABLE admins ADD COLUMN mtls_identity TEXT')
        self.path.chmod(0o600)
    def db(self):return sqlite3.connect(self.path,timeout=5)
    def initialized(self):
        with self.db() as d:return bool(d.execute('SELECT count(*) FROM admins').fetchone()[0])
    def setup_code(self):
        p=self.path.parent/'admin-setup-code.txt'
        if not self.initialized() and not p.exists():
            import os
            fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
        return p
    def event(self,d,actor,event,subject):d.execute('INSERT INTO account_events VALUES(?,?,?,?)',(time.time(),actor,event,subject))
    def register_admin(self,code,name,password):
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9-]{2,31}',name):raise ValueError('管理员账户名需为 3–32 位小写字母、数字或短横线')
        salt=secrets.token_hex(16);hashed=password_hash(password,salt)
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            first=not d.execute('SELECT 1 FROM admins LIMIT 1').fetchone()
            if first:
                p=self.path.parent/'admin-setup-code.txt'
                if not p.exists() or not hmac.compare_digest(p.read_text().strip(),code):raise ValueError('初始化码无效')
            else:
                r=d.execute("SELECT role,expires,used FROM invites WHERE token=?",(digest(code),)).fetchone()
                if not r or r[0]!='admin' or r[1]<=time.time() or r[2]:raise ValueError('管理员邀请码无效或已过期')
                d.execute('UPDATE invites SET used=1 WHERE token=?',(digest(code),))
            d.execute('INSERT INTO admins(name,salt,hash,enabled) VALUES(?,?,?,1)',(name,salt,hashed));self.event(d,name,'admin_registered',name)
        if first:p.unlink(missing_ok=True)
    def login(self,name,password):
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE');d.execute('DELETE FROM login_attempts WHERE time<?',(time.time()-60,))
            if d.execute('SELECT count(*) FROM login_attempts').fetchone()[0]>=10:raise ValueError('尝试过多，请一分钟后再试')
            d.execute('INSERT INTO login_attempts VALUES(?)',(time.time(),))
            row=d.execute('SELECT salt,hash,enabled FROM admins WHERE name=?',(name,)).fetchone()
        salt=row[0] if row else '00'*16
        try:hashed=password_hash(password,salt)
        except ValueError:raise ValueError('账户或密码错误')
        if not row or not row[2] or not hmac.compare_digest(hashed,row[1]):raise ValueError('账户或密码错误')
        token=secrets.token_urlsafe(32)
        with self.db() as d:
            d.execute('INSERT INTO sessions VALUES(?,?,?)',(digest(token),name,time.time()+3600));self.event(d,name,'login',name)
        return token
    def who(self,token):
        with self.db() as d:
            r=d.execute('SELECT a.name FROM sessions s JOIN admins a ON a.name=s.admin WHERE s.token=? AND s.expires>? AND a.enabled=1',(digest(token),time.time())).fetchone()
        return r[0] if r else None
    def logout(self,token):
        with self.db() as d:d.execute('DELETE FROM sessions WHERE token=?',(digest(token),))
    def invite(self,actor,role,projects):
        if role not in {'member','admin'} or not isinstance(projects,list) or (role=='member' and not projects) or not all(isinstance(p,str) and re.fullmatch('[a-z][a-z0-9-]{0,63}',p) for p in projects):raise ValueError('请选择有效项目范围')
        code=secrets.token_urlsafe(32)
        with self.db() as d:
            d.execute('INSERT INTO invites VALUES(?,?,?,?,0)',(digest(code),role,time.time()+86400,json.dumps(projects)));self.event(d,actor,'invite_created',role)
        return dict(version=1,role=role,code=code,expires_at=time.time()+86400)
    def import_member(self,actor,body):
        if set(body)!={'version','code','name','device'} or body['version']!=1:raise ValueError('注册申请格式无效')
        if not all(isinstance(body[k],str) and 1<=len(body[k].strip())<=80 for k in ['name','device']):raise ValueError('姓名与设备名必填')
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE');r=d.execute('SELECT role,expires,projects,used FROM invites WHERE token=?',(digest(body['code']),)).fetchone()
            if not r or r[0]!='member' or r[1]<=time.time() or r[3]:raise ValueError('邀请无效、过期或已使用')
            ident='member-'+secrets.token_hex(6)
            d.execute('INSERT INTO members VALUES(?,?,?,?,?)',(ident,body['name'].strip(),body['device'].strip(),r[2],'pending'))
            d.execute('UPDATE invites SET used=1 WHERE token=?',(digest(body['code']),));self.event(d,actor,'member_registered',ident)
        return ident
    def status(self,actor,ident,state):
        if state not in {'active','disabled','rejected','provisioning'}:raise ValueError('Invalid state')
        with self.db() as d:
            if not d.execute('SELECT 1 FROM members WHERE id=?',(ident,)).fetchone():raise ValueError('Unknown member')
            d.execute('UPDATE members SET state=? WHERE id=?',(state,ident));self.event(d,actor,state,ident)
    def admin_status(self,actor,name,enabled):
        if type(enabled)!=bool:raise ValueError('Invalid state')
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            if name==actor:raise ValueError('不能停用自己的账户')
            if not enabled and d.execute('SELECT count(*) FROM admins WHERE enabled=1').fetchone()[0]<=1:raise ValueError('必须保留一个管理员')
            if d.execute('UPDATE admins SET enabled=? WHERE name=?',(int(enabled),name)).rowcount!=1:raise ValueError('Unknown admin')
            d.execute('DELETE FROM sessions WHERE admin=?',(name,));self.event(d,actor,'admin_status',name)
    def bind_identity(self,actor,name,identity):
        """Bind (or clear) the mTLS administrator identity that this web account authorizes."""
        if not isinstance(identity,str) or (identity and not re.fullmatch('[a-z][a-z0-9-]{0,63}',identity)):raise ValueError('Invalid mTLS identity')
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            if d.execute('SELECT 1 FROM admins WHERE name=?',(name,)).fetchone() is None:raise ValueError('Unknown admin')
            d.execute('UPDATE admins SET mtls_identity=? WHERE name=?',(identity or None,name));self.event(d,actor,'admin_identity_bound',name+':'+(identity or '-'))
        return {'admin':name,'mtls_identity':identity or None}
    def admin_identities(self):
        """Active administrators' bound mTLS identities; a disabled account drops out immediately."""
        with self.db() as d:
            return {r[0] for r in d.execute("SELECT mtls_identity FROM admins WHERE enabled=1 AND mtls_identity IS NOT NULL AND mtls_identity<>''")}
    def change_password(self,name,old_password,new_password):
        with self.db() as d:
            d.execute('BEGIN IMMEDIATE')
            row=d.execute('SELECT salt,hash,enabled FROM admins WHERE name=?',(name,)).fetchone()
            if not row or not row[2] or not hmac.compare_digest(password_hash(old_password,row[0]),row[1]):raise ValueError('Current password incorrect')
            salt=secrets.token_hex(16);hashed=password_hash(new_password,salt)
            d.execute('UPDATE admins SET salt=?,hash=? WHERE name=?',(salt,hashed,name));d.execute('DELETE FROM sessions WHERE admin=?',(name,));self.event(d,name,'password_changed',name)
    def listing(self):
        with self.db() as d:
            d.row_factory=sqlite3.Row
            members=[dict(r,projects=json.loads(r['projects'])) for r in d.execute('SELECT * FROM members ORDER BY rowid DESC LIMIT 100')]
            admins=[dict(r) for r in d.execute('SELECT name,enabled,mtls_identity FROM admins')]
            events=[dict(r) for r in d.execute('SELECT * FROM account_events ORDER BY rowid DESC LIMIT 50')]
        return dict(members=members,admins=admins,events=events)
