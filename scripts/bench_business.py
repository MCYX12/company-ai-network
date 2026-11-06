#!/usr/bin/env python
"""Repeatable business benchmark for the ask_peer path.

Same client entry point as the skill, so the wall-clock number is what a caller sees; the
server-side phase timings and evidence coverage are read from the answer. Sample classes
and attempts are fixed by a samples file, so an old and a new build can be compared with
the same questions, target, model and depth.

    run     --identity ag-b --target ag-a --samples samples.json --repeats 3 --label new \
            --out report-new.json
    compare report-old.json report-new.json

A dry run lists the plan without spending model quota. Failures and timeouts are kept:
reports include every attempt, not only the fast successes.
"""
import argparse
import asyncio
import io
import json
import os
import random
import statistics
import sys
import time
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

DEFAULT_SAMPLES=[
    {'id':'confirm','category':'short_confirm','question':'只回复两个字：收到'},
    {'id':'progress','category':'project_progress','question':'这个项目目前的进展、已完成和阻塞项是什么？'},
    {'id':'long-doc','category':'long_review','question':'请根据随附授权资料，说明算法计划的关键点、规则覆盖范围和版本状态。'},
]


def percentile(values, fraction):
    values=sorted(float(v) for v in values)
    if not values:
        return None
    if len(values)==1:
        return values[0]
    position=(len(values)-1)*fraction
    lower=int(position);upper=min(lower+1,len(values)-1)
    weight=position-lower
    return values[lower]*(1-weight)+values[upper]*weight


def _extract(result):
    # The outer get_task code is the QUERY result; the task's execution code lives under
    # data.result.code. A budget/expiry failure must not be counted as an OK answer.
    extracted={'state':None,'query_code':None,'result_code':None,'timings':None,'answer_chars':0,
               'digested':0,'omitted':0,'truncated':False}
    if not isinstance(result,dict):
        return extracted
    extracted['query_code']=result.get('code')
    data=result.get('data') if isinstance(result.get('data'),dict) else None
    if not data or 'state' not in data:
        return extracted
    extracted['state']=data.get('state')
    extracted['task_id']=data.get('task_id')
    extracted['retry_task_id']=data.get('retry_task_id')
    extracted['retry_of']=data.get('retry_of')
    task_result=data.get('result') if isinstance(data.get('result'),dict) else None
    if task_result is not None:
        extracted['result_code']=task_result.get('code')
    inner=task_result.get('data') if isinstance(task_result,dict) else None
    if not isinstance(inner,dict):
        return extracted
    extracted['timings']=inner.get('timings')
    text=inner.get('text')
    extracted['answer_chars']=len(text) if isinstance(text,str) else 0
    for source in (inner.get('context_sources') or []):
        if isinstance(source,dict) and source.get('kind')=='project_notes':
            coverage=source.get('coverage') or {}
            extracted['digested']+=coverage.get('digested_total',0)
            extracted['omitted']+=coverage.get('omitted_total',0)
            extracted['truncated']=extracted['truncated'] or bool(source.get('truncated'))
    return extracted


PHASES=('prompt_build_seconds','material_seconds','harness_seconds','model_seconds',
        'claim_seconds','execution_seconds','total_seconds','client_overhead_seconds')


def _phase_value(row,phase):
    if phase=='client_overhead_seconds':
        return row.get('client_overhead_seconds')
    timings=row.get('timings') if isinstance(row.get('timings'),dict) else {}
    value=timings.get(phase)
    return value if isinstance(value,(int,float)) else None


def run_attempt(sample,args,runner):
    namespace=SimpleNamespace(identity=args.identity,target=args.target,question=sample['question'],
                              wait=True,key=None,doctor=False,access_status=False,
                              request_access=False,list=False,task_id=None,question_stdin=False)
    started=time.monotonic()
    error=None
    result=None
    try:
        with redirect_stdout(io.StringIO()):
            result=runner(namespace)
    except Exception as exc:                     # every failure is recorded, not dropped
        error='%s: %s' % (type(exc).__name__,exc)
    wall=time.monotonic()-started
    extracted=_extract(result)
    total=(extracted.get('timings') or {}).get('total_seconds') if isinstance(extracted.get('timings'),dict) else None
    # Worker-EXTERNAL remainder: wall minus final Worker total. It can include connect, readiness,
    # approval, queueing, the original task, retries and transport -- NOT a pure return time.
    # Use explicit event boundaries before calling it "return".
    extracted['client_overhead_seconds']=(round(max(0.0,wall-total),3)
                                          if isinstance(total,(int,float)) else None)
    extracted.update({'wall_seconds':round(wall,3),'error':error,
                      'success':bool(error is None and extracted['query_code']=='OK'
                                     and extracted['state']=='succeeded'
                                     and extracted['result_code']=='OK')})
    return extracted


def summarize(attempts):
    def block(rows):
        walls=[r['wall_seconds'] for r in rows]
        ok=[r for r in rows if r['success']]
        phases={}
        for phase in PHASES:
            values=[value for value in (_phase_value(row,phase) for row in rows)
                    if isinstance(value,(int,float))]
            phases[phase]={'n':len(values),'unknown':len(rows)-len(values),
                           'p50_seconds':percentile(values,0.5) if values else None,
                           'p95_seconds':percentile(values,0.95) if values else None}
        return {'n':len(rows),'success':len(ok),'phases':phases,
                'success_rate':round(len(ok)/len(rows),4) if rows else None,
                'p50_seconds':percentile(walls,0.5),'p95_seconds':percentile(walls,0.95),
                'p99_seconds':percentile(walls,0.99),
                'errors':dict(Counter(r['error'] for r in rows if r['error'])),
                'query_codes':dict(Counter(r['query_code'] for r in rows)),
                'result_codes':dict(Counter(r['result_code'] for r in rows)),
                'answer_chars_mean':round(statistics.mean([r['answer_chars'] for r in rows]),1) if rows else None,
                'digested_total':sum(r['digested'] for r in rows),
                'omitted_total':sum(r['omitted'] for r in rows),
                'truncated':sum(1 for r in rows if r['truncated'])}
    categories={}
    for row in attempts:
        categories.setdefault(row['category'],[]).append(row)
    return {'overall':block(attempts),
            'categories':{name:block(rows) for name,rows in sorted(categories.items())}}


def parse_meta(items):
    """Turn repeated \`--meta key=value\` into a dict; a malformed item is refused."""
    out={}
    for item in items or []:
        if '=' not in item:
            raise ValueError('INVALID_META %s' % item)
        key,_,value=item.partition('=')
        out[key.strip()]=value.strip()
    return out


def categories_of(report):
    """Accept either a full \`run\` report or a bare summary; \`run\` nests under summary."""
    if not isinstance(report,dict):
        return {}
    summary=report.get('summary')
    if isinstance(summary,dict):
        return summary.get('categories') or {}
    return report.get('categories') or {}


def compare_reports(old,new):
    old_categories=categories_of(old);new_categories=categories_of(new)
    if not old_categories or not new_categories:
        raise ValueError('NO_CATEGORIES')
    # The same questions, model and material must back both sides or the delta is noise.
    # Comparing only sample ids would accept the same id with a different question.
    if (old.get('samples') or []) != (new.get('samples') or []):
        raise ValueError('SAMPLE_MISMATCH')
    old_meta=old.get('meta') or {};new_meta=new.get('meta') or {}
    if not old_meta or not new_meta:
        raise ValueError('META_REQUIRED')
    if old_meta != new_meta:
        raise ValueError('META_MISMATCH')
    if old.get('target') and new.get('target') and old.get('target')!=new.get('target'):
        raise ValueError('TARGET_MISMATCH')
    out={'categories':{}}
    for name in sorted(set(old_categories) & set(new_categories)):
        before=old_categories[name];after=new_categories[name]
        def delta(key):
            a,b=before.get(key),after.get(key)
            if not a or b is None:
                return None
            return round((b-a)/a*100,1)
        phase_delta={}
        for phase in PHASES:
            a=(before.get('phases') or {}).get(phase,{}).get('p50_seconds')
            b=(after.get('phases') or {}).get(phase,{}).get('p50_seconds')
            phase_delta[phase]=round((b-a)/a*100,1) if a and b is not None else None
        out['categories'][name]={
            'p50_delta_percent':delta('p50_seconds'),
            'p95_delta_percent':delta('p95_seconds'),
            'phase_p50_delta_percent':phase_delta,
            'success_rate_before':before.get('success_rate'),
            'success_rate_after':after.get('success_rate'),
            'coverage_digested_before':before.get('digested_total'),
            'coverage_digested_after':after.get('digested_total'),
            'coverage_omitted_before':before.get('omitted_total'),
            'coverage_omitted_after':after.get('omitted_total')}
    if not out['categories']:
        raise ValueError('NO_COMMON_CATEGORIES')
    return out


def cmd_run(args):
    samples=[{'id':s['id'],'category':s['category'],'question':s['question']}
             for s in (json.loads(Path(args.samples).read_text()) if args.samples else DEFAULT_SAMPLES)]
    if args.dry_run:
        print(json.dumps({'target':args.target,'identity':args.identity,'label':args.label,
                          'repeats':args.repeats,'dry_run':True,
                          'planned_attempts':len(samples)*args.repeats,
                          'samples':samples},ensure_ascii=False,indent=2))
        return 0
    from app import peer_client
    runner=lambda namespace: asyncio.run(peer_client.run(namespace))
    attempts=[]
    for repeat in range(args.repeats):
        order=list(samples)
        # A/B runs interleave old/new attempts so time-of-day and model drift do not favour
        # one side; within a single label this only randomises the sample order.
        if args.shuffle:
            random.shuffle(order)
        for sample in order:
            row=run_attempt(sample,args,runner)
            row.update({'sample_id':sample['id'],'repeat':repeat,'category':sample['category']})
            attempts.append(row)
            print(json.dumps({'sample':sample['id'],'repeat':repeat,'success':row['success'],
                              'wall_seconds':row['wall_seconds'],'state':row['state'],
                              'error':row['error']},ensure_ascii=False),flush=True)
    report={'label':args.label,'target':args.target,'identity':args.identity,
            'repeats':args.repeats,'shuffle':bool(args.shuffle),'samples':samples,
            'meta':parse_meta(args.meta),'generated_at':time.time(),
            'attempts':attempts,'summary':summarize(attempts)}
    Path(args.out).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report['summary'],ensure_ascii=False,indent=2))
    return 0


def cmd_compare(args):
    old=json.loads(Path(args.old).read_text())
    new=json.loads(Path(args.new).read_text())
    try:
        result=compare_reports(old,new)
    except ValueError as error:
        # An empty comparison must be a clear error, never a success with no data.
        print(json.dumps({'error':str(error),
                          'hint':'pass the files written by run; they carry summary.categories'},
                         ensure_ascii=False))
        return 2
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run')
    run.add_argument('--identity',default='ag-b');run.add_argument('--target',default='ag-a')
    run.add_argument('--samples');run.add_argument('--repeats',type=int,default=1)
    run.add_argument('--label',default='run');run.add_argument('--out',default='bench-report.json')
    run.add_argument('--dry-run',action='store_true');run.add_argument('--shuffle',action='store_true')
    run.add_argument('--meta',action='append',default=[],metavar='KEY=VALUE',
                     help='record model/material/version metadata; compare requires it to match')
    run.set_defaults(func=cmd_run)
    compare=sub.add_parser('compare');compare.add_argument('old');compare.add_argument('new')
    compare.set_defaults(func=cmd_compare)
    args=parser.parse_args()
    return args.func(args)


if __name__=='__main__':
    raise SystemExit(main())
