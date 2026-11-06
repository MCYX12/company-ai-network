"""A2A v1.0-compatible gateway for the platform's ask_peer flow (opt-in, experimental).

Exposes a minimal A2A surface so an external A2A client (Microsoft Agent
Framework, AgentScope, OpenAgents, ...) can discover this gateway and ask one of
the platform's enrolled agents a question:

- GET  /.well-known/agent-card.json   -> AgentCard (supportedInterfaces, skills)
- POST /  or /a2a                     -> JSON-RPC: SendMessage, GetTask

Only the unified text Part is supported. Credentials stay out of band (HTTP
headers / mTLS), and every inbound field is untrusted input, so the bridge never
interprets it as an instruction. This module is pure stdlib plus the existing
nats client; it adds no runtime dependency.

Scope and limits (documented, not hidden):
- No streaming (capabilities.streaming is false) and no push notifications.
- GetTask polls the platform ledger; there is no server-side event stream.
- The HTTP layer runs one asyncio.run per request, so throughput is modest; it is
  intended for a controlled intranet gateway, not a public high-QPS endpoint.
"""
import asyncio
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler

PROTOCOL_VERSION = '1.0'
JSONRPC_VERSION = '2.0'
WELL_KNOWN_PATHS = ('/.well-known/agent-card.json', '/.well-known/agent.json')
JSONRPC_PATHS = ('/', '/a2a', '/rpc')
MAX_BODY = 1000000

# Internal ledger state -> A2A TaskState (SCREAMING_SNAKE_CASE per v1.0).
STATE_MAP = {
    'accepted': 'TASK_STATE_SUBMITTED',
    'queued': 'TASK_STATE_SUBMITTED',
    'dispatched': 'TASK_STATE_WORKING',
    'running': 'TASK_STATE_WORKING',
    'cancel_requested': 'TASK_STATE_WORKING',
    'succeeded': 'TASK_STATE_COMPLETED',
    'failed': 'TASK_STATE_FAILED',
    'expired': 'TASK_STATE_FAILED',
    'cancelled': 'TASK_STATE_CANCELED',
}


def task_state(state):
    return STATE_MAP.get(state, 'TASK_STATE_SUBMITTED')


def _iso(value):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value))


def agent_card(url, name='公司内网 Agent 网关', description=None, version='0.9.0-poc', interface_path='/a2a'):
    """A2A v1.0 AgentCard. The card is discovery metadata, not a capability grant."""
    if description is None:
        description = '把公司内网已授权 Agent 的只读问答以 A2A 协议暴露给外部 A2A 客户端。'
    skill_url = url.rstrip('/') + interface_path
    return {
        'name': name,
        'description': description,
        'version': version,
        'supportedInterfaces': [
            {'url': skill_url, 'protocolBinding': 'JSONRPC', 'protocolVersion': PROTOCOL_VERSION},
        ],
        'capabilities': {'streaming': False, 'pushNotifications': False, 'extendedAgentCard': False},
        'defaultInputModes': ['text/plain'],
        'defaultOutputModes': ['text/plain'],
        'skills': [
            {
                'id': 'ask_peer',
                'name': '跨设备问答',
                'description': '向一个获授权的对端 Agent 提问，返回可核对的项目进展回答。',
                'tags': ['question', 'project-progress', 'read-only'],
                'examples': ['本地项目 X 目前进展如何？'],
                'inputModes': ['text/plain'],
                'outputModes': ['text/plain'],
            }
        ],
    }


def error(code, message, data=None):
    payload = {'code': code, 'message': message}
    if data is not None:
        payload['data'] = data
    return {'error': payload}


def jsonrpc_response(request_id, result=None, err=None):
    response = {'jsonrpc': JSONRPC_VERSION, 'id': request_id}
    if err is not None:
        response['error'] = err
    else:
        response['result'] = result
    return response


def parse_send(params):
    """Extract one text question from SendMessage params. Returns None when invalid."""
    if not isinstance(params, dict):
        return None
    message = params.get('message')
    if not isinstance(message, dict):
        return None
    parts = message.get('parts')
    texts = []
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get('text'), str):
                texts.append(part['text'])
    question = '\n'.join(texts).strip()
    if not 1 <= len(question) <= 1200:
        return None
    meta = message.get('metadata')
    if not isinstance(meta, dict):
        meta = params.get('metadata') if isinstance(params.get('metadata'), dict) else {}
    target = meta.get('target') if isinstance(meta.get('target'), str) else None
    return {
        'question': question,
        'target': target,
        'idempotency_key': message.get('messageId') if isinstance(message.get('messageId'), str) else None,
        'context_id': message.get('contextId') if isinstance(message.get('contextId'), str) else None,
    }


def to_task(internal, context_id=None):
    """Map a platform task (TaskStore.public) to an A2A v1.0 Task."""
    if not isinstance(internal, dict):
        return None
    task_id = internal.get('task_id')
    result = internal.get('result') if isinstance(internal.get('result'), dict) else None
    task = {
        'id': task_id,
        'contextId': context_id or task_id,
        'status': {'state': task_state(internal.get('state')), 'timestamp': _iso(time.time())},
    }
    if result:
        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        text = data.get('text')
        if isinstance(text, str) and text:
            task['artifacts'] = [{
                'artifactId': str(task_id) + '-answer',
                'name': 'answer',
                'parts': [{'text': text}],
            }]
        if result.get('code') not in (None, 'OK'):
            task['status']['message'] = {
                'role': 'ROLE_AGENT',
                'messageId': str(task_id) + '-status',
                'parts': [{'text': str(result.get('code'))}],
            }
    return task


def task_stub(task_id, request, state, message=None):
    task = {
        'id': task_id or ('pending-' + str(uuid.uuid4())),
        'contextId': request.get('context_id') or (task_id or str(uuid.uuid4())),
        'status': {'state': state, 'timestamp': _iso(time.time())},
    }
    if message:
        task['status']['message'] = {'role': 'ROLE_AGENT', 'messageId': task['id'] + '-status',
                                     'parts': [{'text': str(message)}]}
    return task


def _is_v1(version):
    """True when the caller speaks A2A v1.0 (v1 wraps SendMessage in a Task)."""
    return isinstance(version, str) and version.split('.', 1)[0] == '1'


async def dispatch(method, params, bridge, a2a_version=None):
    """Return {'result': ...} or {'error': {...}} for one JSON-RPC call."""
    if method in ('SendMessage', 'message/send'):
        return await _send(params, bridge, a2a_version)
    if method in ('GetTask', 'tasks/get'):
        return await _get(params, bridge)
    if method in ('SendStreamingMessage', 'message/stream', 'SubscribeToTask', 'tasks/resubscribe'):
        return error(-32004, 'Streaming is not supported by this gateway')
    return error(-32601, 'Method not found: ' + str(method))


def _send_result(task, a2a_version):
    """v1.0 SendMessage returns a SendMessageResponse; legacy returns a Task."""
    if _is_v1(a2a_version):
        return {'result': {'task': task}}
    return {'result': task}


async def _send(params, bridge, a2a_version=None):
    request = parse_send(params)
    if request is None:
        return error(-32602, 'Invalid params: a message with 1..1200 characters of text is required')
    if bridge is None:
        return error(-32603, 'Gateway has no upstream bridge configured')
    try:
        result = await bridge.send(question=request['question'], target=request['target'],
                                   idempotency_key=request['idempotency_key'])
    except Exception:
        return error(-32603, 'Upstream error')
    if not isinstance(result, dict):
        return error(-32603, 'Upstream error')
    code = result.get('code')
    if code == 'ACCEPTED':
        task_id = (result.get('data') or {}).get('task_id')
        if isinstance(task_id, str):
            try:
                fetched = await bridge.get(task_id=task_id)
            except Exception:
                fetched = None
            if isinstance(fetched, dict) and fetched.get('code') == 'OK':
                return _send_result(to_task(fetched.get('data'), context_id=request['context_id']), a2a_version)
        return _send_result(task_stub(task_id, request, 'TASK_STATE_SUBMITTED'), a2a_version)
    if code == 'APPROVAL_REQUIRED':
        hint = (result.get('data') or {}).get('hint')
        stub = task_stub(None, request, 'TASK_STATE_INPUT_REQUIRED',
                         message=hint or 'Administrator approval required; ask again later.')
        return _send_result(stub, a2a_version)
    return error(-32001, str(code or 'UPSTREAM_ERROR'), data=result.get('data'))


async def _get(params, bridge):
    if not isinstance(params, dict) or not isinstance(params.get('id'), str) or not params['id']:
        return error(-32602, 'Invalid params: id is required')
    if bridge is None:
        return error(-32603, 'Gateway has no upstream bridge configured')
    try:
        result = await bridge.get(task_id=params['id'])
    except Exception:
        return error(-32603, 'Upstream error')
    if not isinstance(result, dict):
        return error(-32603, 'Upstream error')
    if result.get('code') == 'OK' and isinstance(result.get('data'), dict):
        return {'result': to_task(result['data'], context_id=params.get('contextId'))}
    if result.get('code') in ('FORBIDDEN', 'NO_GRANT', 'NO_SCOPE'):
        return error(-32002, 'Task not found or not visible to this caller')
    return error(-32001, str(result.get('code') or 'UPSTREAM_ERROR'), data=result.get('data'))


class NatsBridge:
    """Map A2A calls onto the platform Router's submit_task/get_task ingress actions."""

    def __init__(self, identity, default_target=None, ttl_seconds=240):
        self.identity = identity
        self.default_target = default_target
        self.ttl_seconds = ttl_seconds

    async def _call(self, target, action, payload):
        from .common import connect, request, query
        nc = await connect(self.identity)
        try:
            body = request(self.identity, target)
            body['action'] = action
            body['payload'] = payload
            return await query(nc, self.identity, body)
        finally:
            await nc.close()

    async def send(self, question, target=None, idempotency_key=None):
        target = target or self.default_target
        if not isinstance(target, str) or not target:
            return {'code': 'NO_ELIGIBLE_AGENT', 'data': {'hint': 'No target configured on the A2A gateway'}}
        payload = {'operation': 'ask_peer', 'question': question,
                   'idempotency_key': idempotency_key or str(uuid.uuid4()),
                   'ttl_seconds': self.ttl_seconds}
        return await self._call(target, 'submit_task', payload)

    async def get(self, task_id):
        return await self._call(None, 'get_task', {'task_id': task_id})


class GatewayHTTPRequestHandler(BaseHTTPRequestHandler):
    """Stdlib HTTP surface. The server object carries .bridge and .bearer_token."""

    server_version = 'A2AGateway/1.0'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _base_url(self):
        host = self.headers.get('Host') or ('%s:%s' % self.server.server_address[:2])
        return 'http://' + host

    def _authorized(self):
        token = getattr(self.server, 'bearer_token', None)
        if not token:
            return True
        return self.headers.get('Authorization') == 'Bearer ' + token

    def do_GET(self):
        if self.path.split('?')[0] in WELL_KNOWN_PATHS:
            self._json(200, agent_card(self._base_url()))
            return
        self._json(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path.split('?')[0] not in JSONRPC_PATHS:
            self._json(404, {'error': 'not_found'})
            return
        if not self._authorized():
            self._json(401, jsonrpc_response(None, err=error(-32003, 'Unauthorized')))
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, jsonrpc_response(None, err=error(-32600, 'Invalid or oversized request')))
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except ValueError:
            self._json(400, jsonrpc_response(None, err=error(-32700, 'Parse error')))
            return
        if not isinstance(payload, dict) or payload.get('jsonrpc') != JSONRPC_VERSION or not isinstance(payload.get('method'), str):
            request_id = payload.get('id') if isinstance(payload, dict) else None
            self._json(400, jsonrpc_response(request_id, err=error(-32600, 'Invalid Request')))
            return
        request_id = payload.get('id')
        try:
            outcome = asyncio.run(dispatch(payload['method'], payload.get('params') or {},
                                            getattr(self.server, 'bridge', None),
                                            a2a_version=self.headers.get('A2A-Version')))
        except Exception:
            outcome = error(-32603, 'Internal error')
        if 'error' in outcome:
            self._json(200, jsonrpc_response(request_id, err=outcome['error']))
        else:
            self._json(200, jsonrpc_response(request_id, result=outcome.get('result')))
