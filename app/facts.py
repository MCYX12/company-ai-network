"""Deterministic fact model (assessment 3.1).

Levels are proven independently; a fact's status is the highest level actually
proven. A document claim (docs_claim) never produces tests_pass or accepted.

Evidence carries three distinct clocks so a rebuild can never forge a fresh
verification time:
* evidence_executed_at - when the test/acceptance artifact actually ran;
* evidence_read_at     - when this build read the artifact;
* generated_at         - when this fact record was produced.

verified_at mirrors evidence_executed_at (the moment the level was proven), or
None when no executed evidence backs the item.
"""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEVELS = ['planned', 'claimed_done', 'code_present', 'tests_pass', 'accepted']
LABELS = {'planned': '计划实施', 'claimed_done': '历史声称完成', 'code_present': '当前代码存在',
          'tests_pass': '测试通过', 'accepted': '业务验收通过', 'unknown': '未知'}

_UNSET = object()


def highest(levels):
    for level in reversed(LEVELS):
        if (levels or {}).get(level):
            return level
    return 'unknown'


def fact(item, levels, source='', evidence=None, verified_at=_UNSET, limits='',
         evidence_executed_at=None, evidence_read_at=None, generated_at=None):
    clean = {level: bool((levels or {}).get(level)) for level in LEVELS}
    status = highest(clean)
    if verified_at is _UNSET:
        verified_at = time.time()
    return {'item': item, 'status': status, 'status_label': LABELS.get(status, '未知'),
            'levels': clean, 'source': source, 'evidence': list(evidence or []),
            'verified_at': verified_at, 'limits': limits,
            'evidence_executed_at': evidence_executed_at,
            'evidence_read_at': evidence_read_at,
            'generated_at': generated_at}


def explain(record):
    lines = ['- ' + record['item'] + '：' + record['status_label']]
    lines.append('  等级：' + '，'.join(LABELS[l] + '=' + ('是' if record['levels'][l] else '否') for l in LEVELS))
    if record.get('source'):
        lines.append('  来源：' + record['source'])
    if record.get('evidence'):
        lines.append('  证据：' + '，'.join(record['evidence']))
    if record.get('verified_at'):
        lines.append('  核验时间：' + str(record['verified_at']))
    if record.get('evidence_executed_at'):
        lines.append('  证据执行时间：' + str(record['evidence_executed_at']))
    if record.get('evidence_read_at'):
        lines.append('  证据读取时间：' + str(record['evidence_read_at']))
    if record.get('generated_at'):
        lines.append('  事实生成时间：' + str(record['generated_at']))
    if record.get('limits'):
        lines.append('  限制：' + record['limits'])
    return '\n'.join(lines)


def facts_dir():
    return Path(os.environ.get('POC_FACTS_DIR') or (ROOT / 'facts'))


def load(project_id, directory=None):
    base = Path(directory) if directory else facts_dir()
    path = base / (project_id + '.json')
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def section(project_id, directory=None, budget=None):
    """Render one project's facts. budget (characters) keeps whole entries only.

    An entry is never split from its 限制 line, so a truncation cannot present a
    level without its caveat; the omitted count is stated instead.
    """
    data = load(project_id, directory)
    if not data or not data.get('items'):
        return ''
    header = '项目 ' + project_id + ' 的已核验事实（只按"等级"理解，不得把低等级说成高等级）：'
    if data.get('revision'):
        # 事实文件绑定它计算时的源码版本；换了版本就应重建，不能把旧结论当当前状态。
        header += '（源码版本 %s）' % str(data['revision'])[:12]
    footer = '缺证据或过期时明确说明；不得推算完成率。'
    entries = [explain(record) for record in data['items']]
    if budget is not None:
        kept = []
        used = len(header) + len(footer) + 2
        for entry in entries:
            if used + len(entry) + 1 > int(budget):
                kept.append('（已省略 %d 条事实以控制提示长度；需要时请针对具体项追问）' % (len(entries) - len(kept)))
                break
            kept.append(entry)
            used += len(entry) + 1
        entries = kept
    return '\n'.join([header, *entries, footer])
