"""Prepare an offline membership request. No credentials or web account required."""
import argparse,json,time
from pathlib import Path

def register(invitation,name,device,output):
    if invitation.stat().st_size>16384:raise ValueError('Invitation too large')
    body=json.loads(invitation.read_text())
    if set(body)!={'version','role','code','expires_at'} or body['version']!=1 or body['role']!='member' or body['expires_at']<=time.time():raise ValueError('Member invitation invalid or expired')
    if not isinstance(body['code'],str) or not 20<=len(body['code'])<=128:raise ValueError('Invalid invitation')
    if not all(isinstance(x,str) and 1<=len(x.strip())<=80 for x in [name,device]):raise ValueError('Name and device required')
    with output.open('x') as f:json.dump(dict(version=1,code=body['code'],name=name.strip(),device=device.strip()),f,ensure_ascii=False,indent=2)
    output.chmod(0o600)
    print('注册申请已保存。请交给管理员审核；收到个人接入包后运行 install.command。')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--invitation',type=Path);p.add_argument('--name');p.add_argument('--device');p.add_argument('--output',type=Path,default=Path('registration.json'));a=p.parse_args()
    register(a.invitation or Path(input('成员邀请文件路径：').strip()).expanduser(),a.name or input('姓名：'),a.device or input('设备名称：'),a.output)
