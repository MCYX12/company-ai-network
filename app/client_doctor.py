"""Read-only client diagnostics. No model call, credential contents or secret output."""
import asyncio,os,ssl,time
from urllib.parse import urlsplit
from .common import RUNTIME,connect,request,query

async def diagnose(identity):
    checks=[]
    def add(stage,status,message):checks.append(dict(stage=stage,status=status,message=message))
    certs=RUNTIME/'certs';endpoint=os.environ.get('NATS_URL','')
    try:
        url=urlsplit(endpoint)
        if url.scheme!='tls' or not url.hostname or not url.port or url.username or url.password:raise ValueError()
        for name in ['ca.crt',identity+'.crt',identity+'.key']:
            p=certs/name
            if not p.is_file() or p.is_symlink():raise ValueError()
        ctx=ssl.create_default_context(cafile=str(certs/'ca.crt'));ctx.load_cert_chain(str(certs/(identity+'.crt')),str(certs/(identity+'.key')))
        add('configuration','ok','服务器配置与本机证书可加载')
    except (OSError,ValueError,ssl.SSLError):
        add('configuration','error','配置或证书不可用，请重新导入管理员交付的个人接入包');return {'code':'DIAGNOSTIC','checks':checks}
    try:
        # Certificate public metadata only; no key contents are returned.
        for name in ['ca.crt',identity+'.crt']:
            info=ssl._ssl._test_decode_cert(str(certs/name));remaining=ssl.cert_time_to_seconds(info['notAfter'])-time.time()
            add('certificate','error' if remaining<=0 else 'warning' if remaining<259200 else 'ok',f'{name}：'+('已过期' if remaining<=0 else f'约 {max(0,int(remaining/3600))} 小时后到期'))
    except (OSError,ValueError,ssl.SSLError):add('certificate','warning','无法解析到期时间，连接时仍严格验证证书')
    async def quiet(*_):pass
    nc=None
    try:
        nc=await connect(identity,timeout=float(os.environ.get('AGENT_CONNECT_TIMEOUT','10') or 10),allow_reconnect=False,max_reconnect_attempts=0,error_cb=quiet,disconnected_cb=quiet)
        add('connection','ok','TLS 连接成功')
        async def send(action):
            body=request(identity);body.pop('target_agent_id');body['action']=action;body['payload']={}
            return await query(nc,identity,body)
        directory=await send('list_agents')
        if directory['code']!='OK':
            add('account','error','服务器拒绝当前身份，请管理员检查账户启用状态和登记');return {'code':'DIAGNOSTIC','checks':checks}
        peers=[a['agent_id'] for a in directory['data']['agents'] if a['presence']=='online' and 'ask_peer' in a['available_task_actions']]
        add('account','ok','账户可访问授权目录')
        add('peers','ok' if peers else 'warning','在线可对话智能体：'+('、'.join(peers) if peers else '暂无，请检查目标在线状态和项目范围'))
        grants=await send('list_access')
        if grants['code']=='OK':
            active=[r['target'] for r in grants['data']['requests'] if r['state']=='approved']
            add('authorization','ok' if active else 'warning','已获对话授权：'+('、'.join(active) if active else '暂无；下一步对指定目标申请授权'))
        else:add('authorization','warning','授权状态暂不可查询')
    except (Exception,) as e:
        add('connection','error','连接或查询失败（'+type(e).__name__+'）；请检查网络、服务器地址和证书有效期。此结果不能单独认定是网络故障。')
    finally:
        if nc:await nc.close()
    return {'code':'DIAGNOSTIC','checks':checks}
