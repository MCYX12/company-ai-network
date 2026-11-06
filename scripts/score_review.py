"""Score a full-review report against a private label set.

Heuristic first-pass scorer: matches extracted facts to labelled important facts by character
bigram overlap, then reports accuracy, key-fact recall, citation correctness and plan/claim
upgrade errors. It is a screener, not a substitute for business sign-off; every judgement is
recorded so a human can review specific matches.

    python -m scripts.score_review --report report.json --labels labels.json --out score.json
"""
import argparse
import json
import re
from pathlib import Path

# An upgrade is claiming completion/verification for a plan/claim. Mentioning measurement is
# not itself an upgrade, and a sentence that also negates completion is not one either.
UPGRADE_WORDS=('已实测','已验证','已上线','已完成','已交付','已通过验收','已投入生产','生产可用')
NEGATIONS=('尚未','未完成','未通过','未验收','不一致','存疑','待验证','未上线','未交付')
CLAIM_LEVELS=('planned','documented','declared')


def bigrams(text):
    cleaned=re.sub(r'[\s\W]+','',str(text or ''))
    return {cleaned[index:index+2] for index in range(max(0,len(cleaned)-1))}


def similarity(claim,fact):
    left=bigrams(claim);right=bigrams(fact)
    if not left or not right:
        return 0.0
    return len(left & right)/len(left)


def score(report,labels,threshold=0.5):
    chunks=report.get('chunks') or []
    extracted=[]
    for chunk in chunks:
        for fact in (chunk.get('facts') or []):
            if isinstance(fact,dict):
                extracted.append({'file':chunk.get('file'),'start_line':chunk.get('start_line'),
                                  'end_line':chunk.get('end_line'),'summary':fact.get('summary',''),
                                  'evidence_ids':fact.get('evidence_ids') or []})
    judgements=[];matched_labels=set()
    for item in extracted:
        best=None;best_score=0.0
        for doc in labels.get('docs',[]):
            if doc['file']!=item['file']:
                continue
            for label in doc.get('facts',[]):
                value=similarity(label['text'],item['summary'])
                if value>best_score:
                    best=(label,doc);best_score=value
        supported=best is not None and best_score>=threshold
        if supported:
            matched_labels.add((best[1]['file'],best[0]['id']))
        level=best[0].get('level') if supported else None
        upgraded=bool(supported and level in CLAIM_LEVELS
                      and any(word in item['summary'] for word in UPGRADE_WORDS)
                      and not any(negation in item['summary'] for negation in NEGATIONS))
        cited=bool(item['evidence_ids'])
        judgements.append({**item,'matched':supported,'score':round(best_score,3),
                           'matched_label':(best[0]['id'] if supported else None),
                           'label_level':level,'upgraded':upgraded,'cited':cited})
    important=[(doc['file'],label) for doc in labels.get('docs',[]) for label in doc.get('facts',[])]
    recall=len(matched_labels)/len(important) if important else None
    total=len(judgements)
    accuracy=sum(1 for row in judgements if row['matched'])/total if total else None
    citation=sum(1 for row in judgements if row['matched'] and row['cited'])/max(1,sum(1 for row in judgements if row['matched'])) if total else None
    upgrades=[row for row in judgements if row['upgraded']]
    empty=sum(1 for chunk in chunks if not (chunk.get('facts') or []) and chunk.get('status')=='processed')
    failed=[chunk for chunk in chunks if chunk.get('status')=='failed']
    # These are automated SCREENING rates, not measured quality: label matching is character
    # bigram overlap and citation only checks that an evidence id is present. A human
    # adjudication (see evidence/quality-adjudication-*.json) must give the real numbers.
    return {'metrics':{'heuristic_label_match_rate':accuracy,'key_recall_heuristic':recall,
                       'citation_present_rate':citation,
                       'upgrade_errors':len(upgrades),'extracted_facts':total,
                       'important_facts':len(important),'valid_empty_chunks':empty,
                       'failed_chunks':len(failed)},
            'metric_scope':'automated screening only; not a fact-accuracy verdict',
            'judgements':judgements,
            'missing':[{'file':file,'id':label['id'],'text':label['text']}
                       for file,label in important if (file,label['id']) not in matched_labels],
            'upgrades':upgrades}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True);parser.add_argument('--labels',required=True)
    parser.add_argument('--out',required=True);parser.add_argument('--threshold',type=float,default=0.5)
    args=parser.parse_args()
    report=json.loads(Path(args.report).read_text())
    labels=json.loads(Path(args.labels).read_text())
    result=score(report,labels,args.threshold)
    Path(args.out).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result['metrics'],ensure_ascii=False))


if __name__=='__main__':
    raise SystemExit(main())
