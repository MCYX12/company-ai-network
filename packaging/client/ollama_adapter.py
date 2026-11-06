"""Fixed local Ollama answer command; questions arrive only on stdin.

Uses only an already installed completion model on 127.0.0.1:11434. It does not
pull models, invoke tools, read project files or reuse a conversation context.
"""
import argparse
import json
import re
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

BASE = 'http://127.0.0.1:11434'
MAX_RESPONSE = 1024 * 1024
MAX_PROMPT = 20000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Ollama redirect refused')


def request_json(path, body=None, timeout=100):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    request = Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    # A system proxy must never receive the local prompt.
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('Oversize Ollama response')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('error'):
        raise ValueError('Invalid Ollama response')
    return value


def generate(prompt, model, timeout=100, max_tokens=512):
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT
            or not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,160}', model)
            or model.endswith(':cloud') or not 1 <= timeout <= 300
            or not 1 <= max_tokens <= 2048):
        raise ValueError('Invalid local model request')
    rows = request_json('/api/tags', timeout=timeout).get('models')
    if not isinstance(rows, list) or not any(isinstance(row, dict) and model in
                                           (row.get('name'), row.get('model')) for row in rows):
        raise ValueError('Model is not installed')
    details = request_json('/api/show', {'model': model}, timeout=timeout)
    if (details.get('remote_host') or details.get('remote_model')
            or 'completion' not in (details.get('capabilities') or [])):
        raise ValueError('Not a local completion model')
    result = request_json('/api/generate', {
        'model': model, 'prompt': prompt, 'stream': False, 'keep_alive': '5m',
        'options': {'temperature': 0, 'num_predict': max_tokens, 'num_ctx': 8192},
    }, timeout=timeout)
    text = result.get('response')
    if result.get('done') is not True or not isinstance(text, str) or not text.strip():
        raise ValueError('Incomplete or empty model answer')
    if result.get('done_reason') == 'length':
        raise ValueError('Model answer exceeded token budget')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--timeout', type=float, default=100)
    parser.add_argument('--max-tokens', type=int, default=512)
    args = parser.parse_args()
    try:
        prompt = sys.stdin.read(MAX_PROMPT + 1)
        answer = generate(prompt, args.model, args.timeout, args.max_tokens)
        print(answer['response'])
        return 0
    except (OSError, ValueError, TypeError) as error:
        # Do not echo the prompt, model body, or raw server error into diagnostics.
        print('OLLAMA_ADAPTER_ERROR:' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
