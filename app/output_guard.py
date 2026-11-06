"""Output-side guard for remote-model answers.

Three layers: credential redaction, a red-team canary block, and a heuristic
semantic check (tool/command claims, echo of injected instructions, system-prompt
leaks). The semantic layer is heuristic and documented as such: it flags and can
block, but it is not a content moderator.
"""
import os
import re

SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:api[_-]?key|access[_-]?token|password|passwd|secret)\s*[:=]\s*\S+')
TOOL_CLAIM = re.compile(r'(我已(经)?(执行|运行|调用)(了)?(命令|脚本|工具)|我(已)?读取了(整台|其他|未授权)|执行了命令|ran the (command|script)|executed the (command|script)|i (have )?(read|accessed) (the )?(whole|entire) (disk|computer))', re.IGNORECASE)
INSTRUCTION_ECHO = re.compile(r'(忽略(以上|上述|之前)(全部)?(的)?(规则|指令)|ignore (all )?(previous|above) instructions)', re.IGNORECASE)
SYSTEM_LEAK = re.compile(r'(system prompt|系统提示词|内部指令|my instructions)', re.IGNORECASE)
MAX_CHARS = 16000

BLOCK = {'CANARY'}
REDACT = {'CREDENTIAL', 'OVERSIZE'}
ANNOTATE = {'TOOL_CLAIM', 'INSTRUCTION_ECHO', 'SYSTEM_LEAK'}


def canary_value():
    return os.environ.get('POC_OUTPUT_CANARY') or None


def strict():
    return os.environ.get('POC_OUTPUT_STRICT') == '1'


def scan(text, canary=None):
    if not isinstance(text, str):
        return {'not_text': True}
    canary = canary_value() if canary is None else canary
    findings = {}
    if SECRET.search(text):
        findings['credential'] = True
    if canary and canary in text:
        findings['canary'] = True
    if len(text) > MAX_CHARS:
        findings['oversize'] = True
    return findings


def evaluate(text, canary=None):
    """Classify an answer: action allow | annotate | redact | block and the findings."""
    if not isinstance(text, str):
        return {'risk': 'high', 'findings': ['NOT_TEXT'], 'action': 'block'}
    canary = canary_value() if canary is None else canary
    findings = []
    if SECRET.search(text):
        findings.append('CREDENTIAL')
    if canary and canary in text:
        findings.append('CANARY')
    if TOOL_CLAIM.search(text):
        findings.append('TOOL_CLAIM')
    if INSTRUCTION_ECHO.search(text):
        findings.append('INSTRUCTION_ECHO')
    if SYSTEM_LEAK.search(text):
        findings.append('SYSTEM_LEAK')
    if len(text) > MAX_CHARS:
        findings.append('OVERSIZE')
    found = set(findings)
    if found & BLOCK:
        action = 'block'
    elif strict() and found & ANNOTATE:
        action = 'block'
    elif found & REDACT:
        action = 'redact'
    elif found & ANNOTATE:
        action = 'annotate'
    else:
        action = 'allow'
    risk = 'high' if action == 'block' else ('medium' if action in {'redact', 'annotate'} else 'low')
    return {'risk': risk, 'findings': findings, 'action': action}


def sanitize(text, canary=None, max_chars=MAX_CHARS):
    """Return (text, warnings). Credentials are redacted, oversize is truncated and a
    canary match blocks the answer instead of returning it."""
    if not isinstance(text, str):
        return '', ['NOT_TEXT']
    canary = canary_value() if canary is None else canary
    warnings = []
    clean = SECRET.sub('[已屏蔽：疑似凭据]', text)
    if clean != text:
        warnings.append('CREDENTIAL_REDACTED')
    if canary and canary in clean:
        return '模型输出包含被策略标记的内容，已阻止返回；如有需要请联系管理员核实来源。', warnings + ['CANARY_BLOCKED']
    if len(clean) > max_chars:
        clean = clean[:max_chars] + '\n[输出过长，已截断]'
        warnings.append('TRUNCATED')
    return clean, warnings


def guard_result(code, data):
    """Apply the output policy to a peer answer; one entry point for every source.

    Cross-boundary answers (one-shot harness, packaged device worker, remote
    agent, local model) are untrusted input: a canary match blocks, credentials
    are redacted, oversize is truncated, and semantic findings are annotated.
    Non-OK results and non-text payloads pass through unchanged.
    """
    if code != 'OK' or not isinstance(data, dict) or not isinstance(data.get('text'), str):
        return code, data
    verdict = evaluate(data['text'])
    if verdict['action'] == 'block':
        hint = 'OUTPUT_CANARY_BLOCKED' if 'CANARY' in verdict['findings'] else 'OUTPUT_BLOCKED'
        return 'OUTPUT_BLOCKED', {'hint': hint, 'findings': verdict['findings']}
    clean, warnings = sanitize(data['text'])
    data['text'] = clean
    findings = sorted(set(warnings + verdict['findings']))
    if findings:
        data['output_warnings'] = findings
    if verdict['action'] == 'annotate':
        data['output_review'] = 'semantic output findings require human review'
    return code, data
