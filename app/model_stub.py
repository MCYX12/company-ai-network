"""Deterministic offline model stub for the pure-intranet drill.

This is NOT a real model. It deterministically restates the supplied evidence and refuses to
invent progress, so the platform's model path can be exercised with no public network. Any
answer it produces is labelled as simulated and must not be presented as a model result.
"""
MARKER = 'local_model_stub'


def answer(prompt):
    lines = [line.strip() for line in (prompt or '').splitlines() if line.strip()]
    question = ''
    for line in lines:
        if line.startswith(('问题', 'Question')):
            question = line
            break
    evidence = [line for line in lines if line.startswith(('- ', '* ', '[')) or 'DATA_UNAVAILABLE' in line]
    return ('【本地模型替身（确定性，非真实模型，仅用于离线演练）】\n'
            + '问题：' + (question or '（未识别）') + '\n'
            + '资料条目：' + str(len(evidence)) + ' 条\n'
            + '结论：仅依据平台提供的资料作答；资料不足或标记 DATA_UNAVAILABLE 时明确说明“暂无已发布记录”，不编造进度。\n'
            + '来源：平台证据条目（非实时抓取）。')
