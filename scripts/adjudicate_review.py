"""Recompute review-quality metrics from a versioned human adjudication file.

PASS mode requires verifiable artifacts: the adjudication's report_hash, dataset_hash and
labels_hash are recomputed from the original report, dataset manifest and labels, and the
adjudication must cover the same fact_id set with matching content. Canonical ids are 'file:f1';
recall is the intersection with the gold set. Exit codes: 0 PASS, 1 FAIL, 2 input error.
"""
import argparse
import hashlib
import json
from pathlib import Path

SCHEMA_VERSION=1
VERDICTS=('supported','derived','duplicate','unsupported','contradicted','unresolved')
POSITIVE=('supported','derived')


def canonical_label(label_id):
    if not label_id:
        return None
    value=str(label_id)
    if '@' in value and ':' not in value:
        label,file=value.split('@',1)
        return '%s:%s'%(file,label)
    return value


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:32]


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:32]


def report_facts(report):
    facts=[]
    for chunk in report.get('chunks',[]):
        for fact in (chunk.get('facts') or []):
            facts.append({'fact_id':'f%02d'%(len(facts)+1),'file':chunk.get('file'),
                          'summary':fact.get('summary',''),
                          'evidence_ids':fact.get('evidence_ids') or []})
    return facts


def compute(adjudication,thresholds=None,gold_labels=None,report_facts_list=None,
            dataset=None,labels_hash=None,dataset_dir=None):
    facts=adjudication.get('facts')
    if not isinstance(facts,list):
        raise ValueError('MISSING_FACTS')
    ids=[fact.get('fact_id') for fact in facts]
    if any(not value for value in ids):
        raise ValueError('MISSING_FACT_ID')
    if len(set(ids))!=len(ids):
        raise ValueError('DUPLICATE_FACT_ID')
    if report_facts_list is not None:
        report_ids=[fact['fact_id'] for fact in report_facts_list]
        if set(report_ids)!=set(ids):
            raise ValueError('FACT_ID_SET_MISMATCH')
        by_id={fact['fact_id']:fact for fact in report_facts_list}
        for fact in facts:
            reference=by_id[fact['fact_id']]
            for field in ('file','summary'):
                if field not in fact:
                    raise ValueError('FACT_CONTENT_MISSING %s.%s'%(fact['fact_id'],field))
                if fact[field]!=reference.get(field):
                    raise ValueError('FACT_CONTENT_MISMATCH %s'%fact['fact_id'])
            if 'evidence_ids' not in fact:
                raise ValueError('FACT_EVIDENCE_MISSING %s'%fact['fact_id'])
            if list(fact['evidence_ids'])!=list(reference.get('evidence_ids') or []):
                raise ValueError('FACT_EVIDENCE_MISMATCH %s'%fact['fact_id'])
        if not adjudication.get('report_hash'):
            raise ValueError('REPORT_HASH_MISSING')
        if adjudication['report_hash']!=canonical_hash(report_facts_list):
            raise ValueError('REPORT_HASH_MISMATCH')
    if labels_hash is not None:
        if not adjudication.get('labels_hash'):
            raise ValueError('LABELS_HASH_MISSING')
        if adjudication['labels_hash']!=labels_hash:
            raise ValueError('LABELS_HASH_MISMATCH')
    if dataset is not None:
        if not adjudication.get('dataset_hash'):
            raise ValueError('DATASET_HASH_MISSING')
        if adjudication['dataset_hash']!=canonical_hash(dataset):
            raise ValueError('DATASET_HASH_MISMATCH')
        if dataset.get('labels_sha256') and labels_hash and dataset['labels_sha256']!=labels_hash:
            raise ValueError('DATASET_LABELS_MISMATCH')
        if dataset_dir is not None and dataset.get('docs'):
            for doc in dataset['docs']:
                path=Path(dataset_dir)/doc['name']
                if not path.exists():
                    raise ValueError('DATASET_DOC_MISSING %s'%doc['name'])
                if hashlib.sha256(path.read_bytes()).hexdigest()[:32]!=doc['sha256']:
                    raise ValueError('DATASET_DOC_HASH_MISMATCH %s'%doc['name'])
    counts={verdict:0 for verdict in VERDICTS}
    covered=set();cited=0;upgrades=0
    for fact in facts:
        verdict=fact.get('verdict')
        if verdict is None:
            verdict='unresolved'
        if verdict not in VERDICTS:
            raise ValueError('UNKNOWN_VERDICT %r'%(verdict,))
        counts[verdict]+=1
        if fact.get('citation_ok'):
            cited+=1
        if fact.get('upgrade_error'):
            upgrades+=1
        label=canonical_label(fact.get('label_id'))
        if verdict in POSITIVE and label:
            covered.add(label)
    total=len(facts)
    correct=counts['supported']+counts['derived']
    metrics={'non_redundant_precision':(round(correct/total,4) if total else None),
             'citation_correctness':(round(cited/total,4) if total else None),
             'upgrade_errors':upgrades,'undecided':counts['unresolved'],
             'total_facts':total,'counts':counts}
    if gold_labels is not None:
        gold={canonical_label(label) for label in gold_labels}
        unknown=sorted(covered-gold)
        if unknown:
            raise ValueError('UNKNOWN_LABELS %s'%unknown[:5])
        metrics['key_recall']=(round(len(covered & gold)/len(gold),4) if gold else None)
    else:
        metrics['key_recall']=None
    thresholds=thresholds or {}
    for key,value in thresholds.items():
        if not isinstance(value,(int,float)) or value!=value or not 0<=value<=1:
            raise ValueError('INVALID_THRESHOLD %s=%r'%(key,value))
    if not thresholds:
        return {'metrics':metrics,'thresholds':{},'status':'computed','pass':None,
                'failures':[],'note':'no thresholds: computed only, not an acceptance pass'}
    failures=[]
    for key in thresholds:
        if metrics.get(key) is None:
            failures.append('REQUIRED_METRIC_UNKNOWN_%s'%key.upper())
    if metrics['undecided']:
        failures.append('UNDECIDED_FACTS')
    if metrics['upgrade_errors']:
        failures.append('UPGRADE_ERRORS')
    for key,minimum in thresholds.items():
        value=metrics.get(key)
        if value is not None and value<minimum:
            failures.append('BELOW_%s'%key.upper())
    return {'metrics':metrics,'thresholds':thresholds,'status':('pass' if not failures else 'fail'),
            'pass':not failures,'failures':failures}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adjudication',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--labels');parser.add_argument('--dataset-manifest');parser.add_argument('--report')
    parser.add_argument('--min-non-redundant',type=float);parser.add_argument('--min-recall',type=float)
    parser.add_argument('--min-citation',type=float)
    args=parser.parse_args()
    thresholds={}
    if args.min_non_redundant is not None:thresholds['non_redundant_precision']=args.min_non_redundant
    if args.min_recall is not None:thresholds['key_recall']=args.min_recall
    if args.min_citation is not None:thresholds['citation_correctness']=args.min_citation
    try:
        adjudication=json.loads(Path(args.adjudication).read_text())
        if adjudication.get('schema_version')!=SCHEMA_VERSION:
            raise ValueError('unsupported adjudication schema: %r'%adjudication.get('schema_version'))
        if thresholds and not (args.report and args.labels and args.dataset_manifest):
            raise ValueError('VERIFIABLE_ARTIFACTS_REQUIRED report, labels and dataset-manifest are required for PASS mode')
        gold_labels=None;report_facts_list=None;dataset=None;labels_hash=None
        if args.labels:
            labels=json.loads(Path(args.labels).read_text())
            labels_hash=file_hash(args.labels)
            gold_labels=[label['id']+'@'+doc['file'] for doc in labels.get('docs',[]) for label in doc.get('facts',[])]
        if args.dataset_manifest:
            dataset=json.loads(Path(args.dataset_manifest).read_text())
        if args.report:
            report=json.loads(Path(args.report).read_text())
            report_facts_list=report_facts(report)
        dataset_dir=(Path(args.dataset_manifest).parent if args.dataset_manifest else None)
        result=compute(adjudication,thresholds,gold_labels,report_facts_list,dataset,labels_hash,dataset_dir)
    except (ValueError,OSError,KeyError) as error:
        print(json.dumps({'error':'%s: %s'%(type(error).__name__,error)},ensure_ascii=False))
        return 2
    Path(args.out).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='pass' else 1


if __name__=='__main__':
    raise SystemExit(main())
