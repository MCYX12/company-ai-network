"""Real-model soak: keep asking a device model through the platform for >=30 minutes.

This is the long-stability counterpart of tests/load_capacity.py (which soaks *fixtures*):
here every cycle is a real ask_peer answered by a real model on a real device, so it
covers the whole path: installed client -> mTLS/NATS -> Router -> durable task ledger ->
device worker -> CLI model -> result guard -> ledger -> client.

Opt-in (spends real model quota):

    ALLOW_CLOUD_MODEL_TESTS=1 .venv/bin/python tests/real_model_soak.py --minutes 30

Per cycle we measure task state/code, wall latency, Router RSS/CPU, and a --list heartbeat.
The gate requires the full window to be covered, a healthy success rate on *every* target that
ran (reported per target, not a best-of), no target dropping offline during the window, and
bounded Router RSS growth.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import drill_gate

RUNTIME = ROOT / '.runtime-lan'
CLIENT = Path(os.environ.get('SOAK_CLIENT', '/home/demo/.local/share/CompanyAgentClient/client.py'))
PYTHON = os.environ.get('SOAK_PYTHON', '/usr/bin/python3')
QUESTION = os.environ.get('SOAK_QUESTION', '冒烟：请一句话确认收到（长稳测试）。')
TERMINAL = {'succeeded', 'failed', 'expired', 'cancelled'}


def objects(text):
    found, index, decoder = [], 0, json.JSONDecoder()
    while True:
        start = text.find('{', index)
        if start < 0:
            return found
        try:
            value, end = decoder.raw_decode(text[start:])
        except ValueError:
            index = start + 1
            continue
        found.append(value)
        index = start + end


def task_result(objs):
    return next((o for o in objs if o.get('code') and isinstance(o.get('data'), dict)
                 and 'task_id' in o['data']), None)


def presence_map(objs):
    """target -> presence from one --list response, or None when the listing was unusable.

    Pure so the heartbeat parser is unit-tested without a live platform.
    """
    first = objs[0] if objs else None
    data = first.get('data') if isinstance(first, dict) else None
    agents = data.get('agents') if isinstance(data, dict) else None
    if not isinstance(agents, list):
        return None
    return {a.get('agent_id'): a.get('presence') for a in agents if isinstance(a, dict)}


def offline_in(presence, targets):
    """Targets that are not online in one heartbeat; a missing listing counts as unknown.

    Returns (offline, unknown): an unreachable listing is not a device going offline, so it is
    reported separately instead of being read as an outage.
    """
    if presence is None:
        return [], list(targets)
    offline = [t for t in targets if presence.get(t) == 'offline']
    unknown = [t for t in targets if t not in presence]
    return offline, unknown


def process_sample(pattern, pid=None):
    """RSS (MB) and CPU seconds of the first process whose command matches."""
    try:
        pid = int(pid) if pid is not None else int(subprocess.run(['pgrep', '-f', pattern], capture_output=True, text=True).stdout.split()[0])
    except (ValueError, IndexError):
        return None, None
    try:
        with open('/proc/%d/statm' % pid) as handle:
            rss_pages = int(handle.read().split()[1])
        with open('/proc/%d/stat' % pid) as handle:
            fields = handle.read().rsplit(')', 1)[1].split()
        cpu = (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')
        return round(rss_pages * os.sysconf('SC_PAGE_SIZE') / 1048576, 1), round(cpu, 2)
    except (OSError, IndexError, ValueError):
        return None, None


def ask(target, timeout=300):
    """One real end-to-end question through the installed client; returns a cycle record."""
    started = time.time()
    proc = subprocess.run([PYTHON, str(CLIENT), '--target', target, '--question-stdin', '--wait'],
                          input=QUESTION + '\n', capture_output=True, text=True, timeout=timeout,
                          cwd=str(ROOT / '.runtime'), env=dict(os.environ, AGENT_WAIT_SECONDS='240'))
    objs = objects(proc.stdout)
    result = task_result(objs)
    data = (result or {}).get('data') or {}
    payload = (data.get('result') or {}).get('data') or {}
    return {'target': target, 'state': data.get('state'), 'code': (data.get('result') or {}).get('code'),
            'task_id': data.get('task_id'), 'seconds': round(time.time() - started, 1),
            'chars': len(payload.get('text') or ''), 'source': payload.get('source')}


def heartbeat(targets, timeout=30):
    """One --list call per cycle: the enabled-list snapshot at start/end is not a heartbeat."""
    try:
        proc = subprocess.run([PYTHON, str(CLIENT), '--list'], capture_output=True, text=True,
                              timeout=timeout, cwd=str(ROOT / '.runtime'))
    except (OSError, subprocess.SubprocessError):
        return None
    return presence_map(objects(proc.stdout))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minutes', type=float, default=float(os.environ.get('SOAK_MINUTES', 30)))
    parser.add_argument('--interval', type=float, default=float(os.environ.get('SOAK_INTERVAL', 60)),
                        help='seconds between cycles (each cycle asks every target once)')
    parser.add_argument('--targets', default=os.environ.get('SOAK_TARGETS', 'mac-claude-code-01'))
    parser.add_argument('--min-success', type=float, default=float(os.environ.get('SOAK_MIN_SUCCESS', 0.95)))
    parser.add_argument('--max-rss-growth-mb', type=float, default=50.0)
    parser.add_argument('--note', default='')
    parser.add_argument('--router-pid',type=int,help='Sample this isolated Router instead of the first matching process')
    parser.add_argument('--evidence-dir',type=Path)
    args = parser.parse_args()
    targets = [t.strip() for t in args.targets.split(',') if t.strip()]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    evidence = args.evidence_dir or ROOT / 'evidence' / ('real-model-soak-' + stamp)
    evidence.mkdir(parents=True)
    log_path = evidence / 'cycles.jsonl'
    checks, cycles, heartbeats = [], [], []
    started = time.time()
    deadline = started + args.minutes * 60

    from app.registry import load_registry
    online_before = sorted(a for a, row in load_registry().items() if row.get('enabled'))
    rss_before, cpu_before = process_sample('app.router',args.router_pid)

    with log_path.open('w') as log:
        index = 0
        while time.time() < deadline:
            index += 1
            cycle_started = time.time()
            for target in targets:
                record = ask(target)
                record['cycle'] = index
                record['at'] = datetime.now(timezone.utc).isoformat()
                cycles.append(record)
                log.write(json.dumps(record, ensure_ascii=False) + '\n')
                log.flush()
                print('cycle %d %-20s %-10s %-18s %5.1fs %d chars' %
                      (index, target, record['state'], record['code'], record['seconds'], record['chars']), flush=True)
            presence = heartbeat(targets)
            heartbeats.append({'cycle': index, 'presence': presence,
                               'at': datetime.now(timezone.utc).isoformat()})
            log.write(json.dumps({'cycle': index, 'heartbeat': presence}, ensure_ascii=False) + '\n')
            rss, cpu = process_sample('app.router',args.router_pid)
            log.write(json.dumps({'cycle': index, 'router_rss_mb': rss, 'router_cpu_s': cpu},
                                 ensure_ascii=False) + '\n')
            # 设备侧每日额度（POC_MODEL_CALL_BUDGET，默认 50/身份+目标/UTC 日）在真实测试里很容易打满：
            # 第一周期就全被 BUDGET_EXCEEDED 拦下时立刻收工，不要空转整个窗口（2026-10-05 实测踩过）。
            cycle_rows = [c for c in cycles if c['cycle'] == index]
            if cycle_rows and all(c['code'] == 'BUDGET_EXCEEDED' for c in cycle_rows):
                print('ABORT: 设备侧每日模型额度已耗尽（见 denials 账本）；提高该设备的 '
                      'POC_MODEL_CALL_BUDGET 或改用额度未消耗的目标后重跑。', flush=True)
                (evidence / 'results.json').write_text(json.dumps(
                    {'scope': 'aborted: daily model budget exhausted', 'cycles': cycles,
                     'heartbeats': heartbeats, 'note': args.note}, ensure_ascii=False, indent=2) + '\n')
                raise SystemExit(2)
            wait = args.interval - (time.time() - cycle_started)
            if wait > 0:
                time.sleep(min(wait,max(0.0,deadline-time.time())))

    window = round(time.time() - started, 1)
    rss_after, cpu_after = process_sample('app.router',args.router_pid)
    online_after = sorted(a for a, row in load_registry().items() if row.get('enabled'))
    growth = None if None in (rss_before, rss_after) else round(rss_after - rss_before, 1)

    def rate(rows):
        ok = sum(1 for r in rows if r['state'] == 'succeeded')
        return (ok, len(rows), round(ok / len(rows), 4) if rows else None)

    per_target = {t: rate([c for c in cycles if c['target'] == t]) for t in targets}
    latency = sorted(c['seconds'] for c in cycles if c['state'] == 'succeeded')
    percentiles = {p: (latency[min(len(latency) - 1, int(round(p / 100 * len(latency) + 0.5)) - 1)] if latency else None)
                   for p in (50, 95)}

    def check(name, ok, detail=None):
        checks.append(drill_gate.record(name, drill_gate.PASS if ok else drill_gate.FAIL, detail))
        print(('PASS ' if ok else 'FAIL ') + name
              + ((' | ' + json.dumps(detail, ensure_ascii=False)[:200]) if detail is not None else ''), flush=True)
        return bool(ok)

    check('窗口覆盖 ≥%.0f 分钟（真实模型持续作业）' % args.minutes, window >= args.minutes * 60,
          {'window_s': window, 'cycles': index})
    check('每个目标都完成了全部周期', all(v[1] == index for v in per_target.values()),
          {t: v[1] for t, v in per_target.items()})
    # 成功率按每个必测目标分别判定：一个目标跑得好不能替另一个目标背书。
    for target in targets:
        ok, runs, target_rate = per_target[target]
        threshold = args.min_success * 100
        name = '目标 %s 成功率 ≥ %.0f%%' % (target, threshold)
        if runs == 0:
            checks.append(drill_gate.blocked(name, '窗口内没有完成任何周期（目标不可达？）'))
        else:
            check(name, target_rate is not None and target_rate >= args.min_success,
                  {'ok': ok, 'runs': runs, 'rate': target_rate})
    check('所有任务都到终态且没有卡在中间状态',
          all(c['state'] in TERMINAL for c in cycles), {'states': sorted({c['state'] for c in cycles})})
    check('运行期间 enabled 名单没有变化', online_before == online_after,
          {'before': len(online_before), 'after': len(online_after)})
    # 周期心跳：只看开始/结束的 enabled 名单会漏掉中途掉线又恢复的目标。
    for target in targets:
        samples = [h['presence'] for h in heartbeats]
        offline = [h['cycle'] for h in heartbeats if target in offline_in(h['presence'], [target])[0]]
        unknown = [h['cycle'] for h in heartbeats if target in offline_in(h['presence'], [target])[1]]
        name = '目标 %s 全程在线（周期心跳）' % target
        if not samples or len(unknown) == len(samples):
            checks.append(drill_gate.blocked(name, '周期心跳未取到 --list 结果（列举失败）',
                                             cycles=len(samples)))
        elif offline:
            checks.append(drill_gate.record(name, drill_gate.FAIL,
                                            {'offline_cycles': offline[:10], 'unknown_cycles': len(unknown)}))
        else:
            checks.append(drill_gate.record(name, drill_gate.PASS,
                                            {'samples': len(samples), 'unknown_cycles': len(unknown)}))
    budget_hits = sum(1 for c in cycles if c['code'] == 'BUDGET_EXCEEDED')
    check('没有因设备侧每日模型额度被拦（BUDGET_EXCEEDED=0）', budget_hits == 0,
          {'budget_exceeded': budget_hits,
           'hint': '设备侧 POC_MODEL_CALL_BUDGET 默认 50/身份+目标/UTC 日'})
    check('Router 内存增长 ≤ %.0fMB' % args.max_rss_growth_mb,
          growth is not None and growth <= args.max_rss_growth_mb,
          {'rss_before_mb': rss_before, 'rss_after_mb': rss_after, 'growth_mb': growth,
           'cpu_s': None if None in (cpu_before, cpu_after) else round(cpu_after - cpu_before, 1)})

    counts = drill_gate.tally(checks)
    result = {'scope': '≥%.0f 分钟真实模型长稳（每个周期经真实客户端与真实模型完成一次 ask_peer）' % args.minutes,
              'targets': targets, 'question': QUESTION, 'started_at': stamp, 'window_seconds': window,
              'cycles': index, 'per_target': {t: {'ok': v[0], 'runs': v[1], 'rate': v[2]} for t, v in per_target.items()},
              'latency_seconds': percentiles, 'router_rss_mb': [rss_before, rss_after],
              'heartbeat_cycles': len(heartbeats),
              'identities': {'before': online_before, 'after': online_after}, 'checks': checks,
              'passed': counts['passed'], 'failed': counts['failed'], 'skipped': counts['skipped'],
              'blocked': counts['blocked'], 'info': counts['info'], 'total': counts['total'],
              'note': args.note}
    (evidence / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('\n== 长稳 %d 项：PASS %d / FAIL %d / SKIP %d / BLOCKED %d | 窗口 %.0fs | P50 %ss P95 %ss =='
          % (counts['total'], counts['passed'], counts['failed'], counts['skipped'], counts['blocked'],
             window, percentiles[50], percentiles[95]), flush=True)
    print('Evidence:', evidence, flush=True)
    raise SystemExit(drill_gate.finish('real_model_soak', checks, evidence))


if __name__ == '__main__':
    if os.environ.get('ALLOW_CLOUD_MODEL_TESTS') != '1':
        print('SKIP: explicit ALLOW_CLOUD_MODEL_TESTS=1 required (this drill spends model quota)')
        raise SystemExit(0)
    main()
