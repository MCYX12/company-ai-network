import asyncio
import json
import os
from pathlib import Path
import ssl
import time
import uuid
import nats

from .site import load as _site_load

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = Path(os.environ.get('POC_RUNTIME', ROOT / '.runtime'))


def authority_base(runtime=None):
    """Single authorization/identity authority for every store.

    In HA every Router sets POC_AUTHZ_RUNTIME to the same shared directory, so a revocation
    there is what every Router reads; single-host deployments keep the local runtime. The
    caller passes its own RUNTIME so a patched module-level RUNTIME is still honored.
    """
    shared = os.environ.get('POC_AUTHZ_RUNTIME')
    return Path(shared) if shared else (runtime or RUNTIME)
# Company-specific values come from the site profile; the defaults reproduce the
# original single-site deployment.
_SITE = _site_load()
PREFIX = _SITE.get('prefix') or 'corp.dev.v1'
_DEFAULTS = _SITE.get('defaults', {})

# An approval may be granted without a time limit. It is stored as a fixed
# far-future expiry so every existing "expires > now" check keeps working
# unchanged; an administrator passing 0 seconds means this value.
NEVER_EXPIRES = 4102444800  # 2100-01-01T00:00:00Z


def approval_expiry(seconds, now=None):
    now = time.time() if now is None else now
    return NEVER_EXPIRES if seconds == 0 else now + seconds

def encode(value):
    return json.dumps(value, allow_nan=False).encode()

def decode(raw):
    return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))

def request(sender=None, target=None, action='get_project_progress', project=None):
    sender = sender or _DEFAULTS.get('caller', 'ag-a')
    target = target if target is not None else _DEFAULTS.get('target', 'ag-b')
    project = project if project is not None else _DEFAULTS.get('project')
    return dict(schema_version='1.0', message_id=str(uuid.uuid4()), correlation_id=str(uuid.uuid4()),
                sender_agent_id=sender, target_agent_id=target, action=action,
                expires_at=time.time()+5, payload={'project_id': project} if project else {})

def parse_servers(raw=None):
    """Split a comma-separated NATS_URL into the server LIST nats-py requires.

    A multi-host deployment exports 'tls://a:14222,tls://b:14222'; passing the whole string as
    one element makes the client parse the port as '14222,tls:' and fail.
    """
    raw = raw if raw is not None else os.environ.get('NATS_URL', 'tls://localhost:14222')
    return [server.strip() for server in str(raw).split(',') if server.strip()]


async def connect(identity, timeout=None, **kwargs):
    """Connect to the local server.

    timeout bounds only the initial connect and is for short-lived clients: nats-py
    otherwise keeps cycling its server pool when the endpoint is refused, unreachable
    or black-holed, so a CLI printed nothing and looked hung for minutes. Long-running
    services pass no timeout and keep waiting for the server, as before.
    """
    certs = RUNTIME / 'certs'
    context = ssl.create_default_context(cafile=str(certs/'ca.crt'))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.load_cert_chain(certs/f'{identity}.crt', certs/f'{identity}.key')
    async def error_cb(error):
        print(json.dumps({'identity': identity, 'nats_error': str(error)}), flush=True)
    async def disconnected_cb():
        print(json.dumps({'identity': identity, 'connection': 'disconnected',
                          'detail': 'retrying automatically'}), flush=True)
    async def reconnected_cb():
        print(json.dumps({'identity': identity, 'connection': 'reconnected'}), flush=True)
    # A multi-host deployment exports a comma-separated NATS_URL; nats-py needs a server LIST,
    # otherwise 'tls://a:14222,tls://b:14222' is parsed as one host and fails on the port.
    options = dict(servers=parse_servers(), tls=context,
                   name=identity, connect_timeout=2, max_reconnect_attempts=-1,
                   reconnect_time_wait=1, ping_interval=5, max_outstanding_pings=2,
                   error_cb=error_cb, disconnected_cb=disconnected_cb,
                   reconnected_cb=reconnected_cb)
    options.update(kwargs)
    if timeout is None or float(timeout) <= 0:
        return await nats.connect(**options)
    return await asyncio.wait_for(nats.connect(**options), float(timeout))

async def query(nc, identity, body, timeout=6):
    future = asyncio.get_running_loop().create_future()
    async def receive(msg):
        data = decode(msg.data)
        if data.get('correlation_id') == body['correlation_id'] and not future.done():
            future.set_result(data)
    sub = await nc.subscribe(f'{PREFIX}.client.{identity}.response', cb=receive)
    try:
        await nc.flush()
        await nc.publish(f'{PREFIX}.ingress.{identity}.request', encode(body))
        return await asyncio.wait_for(future, timeout)
    finally:
        await sub.unsubscribe()
