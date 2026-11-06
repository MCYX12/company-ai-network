"""A2A gateway: AgentCard, Task mapping, JSON-RPC dispatch and HTTP surface."""
import asyncio
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from app.a2a_gateway import (GatewayHTTPRequestHandler, agent_card, dispatch, parse_send,
                             task_state, to_task)


class FakeBridge:
    def __init__(self, send_result=None, get_result=None):
        self.send_result = send_result if send_result is not None else {'code': 'ACCEPTED', 'data': {'task_id': 't1'}}
        self.get_result = get_result if get_result is not None else {
            'code': 'OK', 'data': {'task_id': 't1', 'state': 'succeeded',
                                   'result': {'code': 'OK', 'data': {'text': 'answer text'}}}}
        self.sent = []

    async def send(self, question, target=None, idempotency_key=None):
        self.sent.append({'question': question, 'target': target, 'idempotency_key': idempotency_key})
        return self.send_result

    async def get(self, task_id):
        return self.get_result


def send_params(text='进展如何', target=None, message_id='m1'):
    message = {'role': 'user', 'messageId': message_id, 'parts': [{'text': text}]}
    if target:
        message['metadata'] = {'target': target}
    return {'message': message}


class CardTests(unittest.TestCase):
    def test_card_uses_a2a_v1_shape(self):
        card = agent_card('http://127.0.0.1:8099')
        self.assertEqual(card['supportedInterfaces'][0]['protocolVersion'], '1.0')
        self.assertEqual(card['supportedInterfaces'][0]['protocolBinding'], 'JSONRPC')
        self.assertFalse(card['capabilities']['streaming'])
        self.assertEqual(card['skills'][0]['id'], 'ask_peer')

    def test_task_state_mapping(self):
        self.assertEqual(task_state('running'), 'TASK_STATE_WORKING')
        self.assertEqual(task_state('succeeded'), 'TASK_STATE_COMPLETED')
        self.assertEqual(task_state('cancelled'), 'TASK_STATE_CANCELED')
        self.assertEqual(task_state('failed'), 'TASK_STATE_FAILED')


class ParseTests(unittest.TestCase):
    def test_parses_text_and_metadata(self):
        parsed = parse_send(send_params('hello', target='peer-x'))
        self.assertEqual(parsed['question'], 'hello')
        self.assertEqual(parsed['target'], 'peer-x')
        self.assertEqual(parsed['idempotency_key'], 'm1')

    def test_rejects_empty_and_oversize(self):
        self.assertIsNone(parse_send(send_params('')))
        self.assertIsNone(parse_send(send_params('x' * 1201)))
        self.assertIsNone(parse_send({}))


class DispatchTests(unittest.TestCase):
    def test_send_message_returns_completed_task(self):
        bridge = FakeBridge()
        outcome = asyncio.run(dispatch('SendMessage', send_params('进展'), bridge))
        task = outcome['result']
        self.assertEqual(task['status']['state'], 'TASK_STATE_COMPLETED')
        self.assertEqual(task['artifacts'][0]['parts'][0]['text'], 'answer text')
        self.assertEqual(bridge.sent[0]['question'], '进展')

    def test_legacy_method_name_alias(self):
        outcome = asyncio.run(dispatch('message/send', send_params(), FakeBridge()))
        self.assertIn('result', outcome)

    def test_approval_required_maps_to_input_required(self):
        bridge = FakeBridge(send_result={'code': 'APPROVAL_REQUIRED', 'data': {'hint': 'need approval'}})
        outcome = asyncio.run(dispatch('SendMessage', send_params(), bridge))
        self.assertEqual(outcome['result']['status']['state'], 'TASK_STATE_INPUT_REQUIRED')

    def test_status_message_uses_canonical_proto_enum_names(self):
        """proto3 JSON accepts only the exact enum name.

        ``'role': 'agent'`` made an official a2a-sdk 1.2.1 client raise ParseError — on
        exactly those tasks that carry a failure message, i.e. the client could not see why
        its task failed. Verified against protobuf 7.36.2: 'agent' and 'AGENT' are rejected,
        'ROLE_AGENT' and the number 1 are accepted.
        """
        from app.a2a_gateway import task_stub
        failed = to_task({'task_id': 't1', 'state': 'failed',
                          'result': {'code': 'EXECUTION_FAILED', 'data': {'text': 'boom'}}})
        stub = task_stub('t1', {}, 'TASK_STATE_INPUT_REQUIRED', message='need approval')
        for task in (failed, stub):
            message = task['status']['message']
            self.assertEqual(message['role'], 'ROLE_AGENT')
            self.assertNotIn('kind', message)  # A2A 1.0 的消息信封没有 kind 字段
            self.assertTrue(task['status']['state'].startswith('TASK_STATE_'))
            self.assertRegex(task['status']['timestamp'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual(failed['status']['message']['parts'][0]['text'], 'EXECUTION_FAILED')

    def test_upstream_error_becomes_jsonrpc_error(self):
        bridge = FakeBridge(send_result={'code': 'NO_ELIGIBLE_AGENT', 'data': {'hint': 'none'}})
        outcome = asyncio.run(dispatch('SendMessage', send_params(), bridge))
        self.assertEqual(outcome['error']['code'], -32001)

    def test_get_task_and_unknown_method(self):
        outcome = asyncio.run(dispatch('GetTask', {'id': 't1'}, FakeBridge()))
        self.assertEqual(outcome['result']['id'], 't1')
        self.assertEqual(asyncio.run(dispatch('Nope', {}, FakeBridge()))['error']['code'], -32601)
        self.assertEqual(asyncio.run(dispatch('SendStreamingMessage', {}, FakeBridge()))['error']['code'], -32004)

    def test_invalid_params(self):
        outcome = asyncio.run(dispatch('SendMessage', {}, FakeBridge()))
        self.assertEqual(outcome['error']['code'], -32602)
        self.assertEqual(asyncio.run(dispatch('GetTask', {}, FakeBridge()))['error']['code'], -32602)

    def test_v1_send_wraps_in_send_message_response(self):
        # A2A v1.0 SendMessage returns SendMessageResponse { task }, not a bare Task.
        outcome = asyncio.run(dispatch('SendMessage', send_params('进展'), FakeBridge(), a2a_version='1.0'))
        self.assertIn('task', outcome['result'])
        self.assertEqual(outcome['result']['task']['status']['state'], 'TASK_STATE_COMPLETED')

    def test_legacy_send_stays_a_bare_task(self):
        outcome = asyncio.run(dispatch('SendMessage', send_params('进展'), FakeBridge()))
        self.assertNotIn('task', outcome['result'])


class HttpTests(unittest.TestCase):
    def _serve(self, bridge, bearer_token=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), GatewayHTTPRequestHandler)
        server.bridge = bridge
        server.bearer_token = bearer_token
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_agent_card_and_send_over_http(self):
        server = self._serve(FakeBridge())
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        card = json.load(urllib.request.urlopen(base + '/.well-known/agent-card.json'))
        self.assertEqual(card['skills'][0]['id'], 'ask_peer')
        payload = {'jsonrpc': '2.0', 'id': 7, 'method': 'SendMessage', 'params': send_params('问题')}
        http = urllib.request.Request(base + '/', data=json.dumps(payload).encode(),
                                      headers={'Content-Type': 'application/json'})
        response = json.load(urllib.request.urlopen(http))
        self.assertEqual(response['id'], 7)
        self.assertEqual(response['result']['status']['state'], 'TASK_STATE_COMPLETED')

    def test_v1_header_gets_envelope_over_http(self):
        server = self._serve(FakeBridge())
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        payload = {'jsonrpc': '2.0', 'id': 2, 'method': 'SendMessage', 'params': send_params('问题')}
        http = urllib.request.Request(base + '/', data=json.dumps(payload).encode(),
                                      headers={'Content-Type': 'application/json', 'A2A-Version': '1.0'})
        response = json.load(urllib.request.urlopen(http))
        self.assertEqual(response['result']['task']['status']['state'], 'TASK_STATE_COMPLETED')

    def test_bearer_token_required_when_configured(self):
        server = self._serve(FakeBridge(), bearer_token='sekret')
        base = 'http://127.0.0.1:%d' % server.server_address[1]
        payload = {'jsonrpc': '2.0', 'id': 1, 'method': 'GetTask', 'params': {'id': 't1'}}
        http = urllib.request.Request(base + '/', data=json.dumps(payload).encode(),
                                      headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(http)
        self.assertEqual(ctx.exception.code, 401)


if __name__ == '__main__':
    unittest.main()
