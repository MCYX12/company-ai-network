#!/usr/bin/env python
"""Full review of one authorized root: manifest by default, per-chunk facts with --extract.

Authorization must be explicit: either a caller/target pair whose central grant covers the
root, or an explicit --owner-local review by the device owner. The document list reuses the
profile's path rules, and authorization is re-checked before every chunk and before the
report is delivered, so a revoked grant cannot keep producing output.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from app import doc_review
from app.directory_requests import (DirectoryRequests, RootRejected, reason_text,
                                    reject_code, validate_root)


def parse_facts(text):
    """Facts from a model response, or raise on unparseable/refused output.

    An unparseable or refused answer is NOT an empty review: it must become a failed chunk
    so it can never make the whole review look complete. Only a schema-shaped object with a
    \`facts\` list (possibly empty) counts as a valid result.
    """
    cleaned=(text or '').strip()
    if cleaned.startswith('```'):
        cleaned='\n'.join(line for line in cleaned.splitlines() if not line.startswith('```')).strip()
    value=None
    try:
        value=json.loads(cleaned)
    except ValueError:
        start=cleaned.find('{');end=cleaned.rfind('}')
        if start>=0 and end>start:
            try: value=json.loads(cleaned[start:end+1])
            except ValueError: value=None
    if value is None:
        raise ValueError('PARSE_FAILED')
    if not isinstance(value,dict) or not isinstance(value.get('facts'),list):
        raise ValueError('STRUCTURE_INVALID')
    return value['facts']


_FACT_FORMAT={'type':'object','properties':{'facts':{'type':'array','maxItems':3,'items':{
    'type':'object','properties':{'summary':{'type':'string','maxLength':100},
                                   'basis':{'type':'string','enum':['documented']},
                                   'evidence_ids':{'type':'array','maxItems':1,
                                                   'items':{'type':'string','enum':['E1']}}},
    'required':['summary','basis','evidence_ids'],'additionalProperties':False}}},
    'required':['facts'],'additionalProperties':False}


def local_extractor(chunk):
    from scripts.summarize_local_project import MODEL,guard_facts,request
    prompt=('只根据下面这一节资料，用中文提取最多 3 条互不重复的事实，'
            '每条 100 字以内；资料不足就不输出。'
            '返回 JSON {"facts":[{"summary":"...","basis":"documented","evidence_ids":["E1"]}]}。\n资料：\n'
            +chunk['text'][:8000])
    response=request('/api/generate',{'model':MODEL,'prompt':prompt,'stream':False,'think':False,
                                       'format':_FACT_FORMAT,
                                       'options':{'temperature':0,'num_predict':600}})
    facts=parse_facts(response.get('response'))
    if not facts:
        return []
    evidence=[{'id':'E1','kind':'current_project_document','text':chunk['text']}]
    return guard_facts(facts,evidence)


def within(root,granted):
    """True when the ACTUAL path of `root` is inside the actual path of `granted`.

    Real paths are compared after resolving symlinks, so a string prefix such as
    `/approved/demo/../other` can no longer pass as if it were inside `/approved/demo`.
    """
    actual=os.path.realpath(root)
    bound=os.path.realpath(granted)
    return actual==bound or actual.startswith(bound.rstrip('/')+'/')


def authorized(caller,target,root):
    # Reject a traversal/non-normalized root before it can be prefix-matched against a grant.
    try:
        validate_root(root)
    except RootRejected:
        return False
    try:
        rows=DirectoryRequests().listing(caller,target)
    except Exception:
        return False
    for row in rows:
        if row.get('state')!='approved':
            continue
        granted=row.get('root','') or ''
        if granted and within(root,granted):
            return True
    return False


def resolve_docs(raw):
    """Validate a manual document list with the same rules as the profile."""
    if not raw:
        return None
    from app.local_progress import _clean_doc
    docs=[]
    for token in raw.split(','):
        clean=_clean_doc(token.strip())
        if not clean:
            raise ValueError('INVALID_DOC %s' % token.strip())
        docs.append(clean)
    return docs


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True)
    parser.add_argument('--caller');parser.add_argument('--target')
    parser.add_argument('--owner-local',action='store_true',
                        help='device owner reviews their own granted root; no central grant lookup')
    parser.add_argument('--allowed-docs');parser.add_argument('--max-chunks',type=int,default=200)
    parser.add_argument('--extract',action='store_true')
    parser.add_argument('--allow-partial',action='store_true',
                        help='exit 0 even when chunks failed or were omitted')
    parser.add_argument('--out')
    args=parser.parse_args()
    if bool(args.caller)!=bool(args.target):
        print(json.dumps({'error':'CALLER_TARGET_TOGETHER',
                          'hint':'pass both --caller and --target'},ensure_ascii=False))
        return 2
    if not args.caller and not args.owner_local:
        print(json.dumps({'error':'AUTHZ_REQUIRED',
                          'hint':'pass --caller and --target, or --owner-local'},ensure_ascii=False))
        return 2
    try:
        root=validate_root(args.root)
    except RootRejected as error:
        code=reject_code(error)
        print(json.dumps({'error':'INVALID_ROOT','code':code,'detail':reason_text(code),
                          'root':args.root},ensure_ascii=False))
        return 2
    # Bind the review to the same resolved root the authorization check used.
    root=os.path.realpath(root)
    try:
        docs=resolve_docs(args.allowed_docs)
    except ValueError as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False))
        return 2
    def authz():
        if args.caller and args.target and not authorized(args.caller,args.target,root):
            raise PermissionError('NOT_AUTHORIZED')
    if args.caller and args.target and not authorized(args.caller,args.target,root):
        print(json.dumps({'error':'NOT_AUTHORIZED','root':root},ensure_ascii=False))
        return 2
    report=doc_review.review_root(root,docs=docs,
                                  extractor=local_extractor if args.extract else None,
                                  max_chunks=args.max_chunks,
                                  authz=authz if args.caller else None)
    if report.get('revoked'):
        # A revoked authorization is an independent refusal path: no fact produced before the
        # revocation may reach stdout or --out, and --allow-partial cannot bypass it.
        print(json.dumps({'error':'AUTHORIZATION_REVOKED','root':args.root,
                          'detail':report['revoked'],'summary':report.get('summary'),
                          'facts_withheld':report.get('facts_withheld')},ensure_ascii=False))
        return 4
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    if not report.get('complete') and not args.allow_partial:
        print(json.dumps({'incomplete':True,'summary':report['summary'],
                          'hint':'failed/omitted chunks, a lost authorization or skipped '
                                 'documents; use --allow-partial to accept a partial review'},
                         ensure_ascii=False),file=sys.stderr)
        return 3
    return 0


if __name__=='__main__':
    raise SystemExit(main())
