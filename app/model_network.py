"""Optional administrator-owned, process-scoped proxy for Codex model calls."""
import json
import os
from urllib.parse import urlsplit
from .common import RUNTIME


def model_environment(tool):
    env=os.environ.copy()
    if tool!='codex':return env
    path=RUNTIME/'model-network.json'
    if not path.exists():return env
    if path.is_symlink() or path.stat().st_mode&0o022:raise ValueError('Unsafe model network configuration')
    raw=path.read_bytes()
    if len(raw)>4096:raise ValueError('Oversize model network configuration')
    config=json.loads(raw)
    if not isinstance(config,dict) or set(config)!={'codex_proxy'}:raise ValueError('Unexpected model network configuration')
    proxy=config['codex_proxy']
    if not isinstance(proxy,str):raise ValueError('Invalid proxy URL')
    parsed=urlsplit(proxy)
    if parsed.scheme!='http' or parsed.hostname not in {'127.0.0.1','::1'} or not parsed.port or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:raise ValueError('Only a credential-free loopback HTTP proxy is allowed')
    for key in ['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','WS_PROXY','WSS_PROXY','ws_proxy','wss_proxy']:env[key]=proxy
    bypass=env.get('NO_PROXY',env.get('no_proxy',''))
    env['NO_PROXY']=env['no_proxy']=','.join(filter(None,[bypass,'localhost','127.0.0.1','::1']))
    return env
