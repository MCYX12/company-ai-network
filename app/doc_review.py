"""Full review of an authorized root: every policy chunk has a recorded outcome.

Fast question answering embeds a few ranked chunks; a full review needs proof that every
authorized chunk was either processed, explicitly omitted for a bounded reason, or failed.
This module produces that record. A model extractor is optional so the manifest can be
produced (and tested) without a local model.
"""
from .local_progress import _document_chunks, notes_docs


def review_root(root,docs=None,extractor=None,max_chunks=200,authz=None):
    """Review one root; `authz` (if given) gates the read, every model call and delivery.

    Authorization is checked BEFORE any document is opened, then before each file read and
    each model call. A revoked authorization stops the review, and no fact produced before
    the revocation is returned, so a long review cannot keep delivering output after access
    is withdrawn.
    """
    names=list(docs) if docs is not None else list(notes_docs())
    def check():
        if authz is None:
            return None
        try:
            authz()
            return None
        except Exception as exc:
            return '%s: %s'%(type(exc).__name__,str(exc)[:200])
    # 1) Authorize before reading anything.
    revoked=check()
    chunks=[];skipped=[]
    if revoked is None:
        # 2) Read one document at a time, re-checking authorization before each read, so a
        # revocation during a long review cannot keep opening later documents.
        for name in names:
            revoked=check()
            if revoked:
                break
            part,part_skipped=_document_chunks(root,[name])
            chunks.extend(part);skipped.extend(part_skipped)
    results=[]
    def record_for(chunk,status,reason=None,facts=None):
        record={'file':chunk['file'],'heading':chunk['heading'],
                'start_line':chunk['start_line'],'end_line':chunk['end_line'],
                'sha256':chunk['sha256'],'chars':chunk['chars'],'status':status}
        if reason:record['reason']=reason
        if facts is not None:record['facts']=facts
        return record
    if revoked is None:
        for index,chunk in enumerate(chunks):
            revoked=check()
            if revoked:
                for later in chunks[index:]:
                    results.append(record_for(later,'failed','AUTHORIZATION_LOST: '+revoked))
                break
            if index>=max_chunks:
                results.append(record_for(chunk,'omitted','CHUNK_LIMIT'))
            elif extractor is None:
                results.append(record_for(chunk,'manifest'))
            else:
                try:
                    results.append(record_for(chunk,'processed',facts=extractor(chunk)))
                except Exception as exc:
                    results.append(record_for(chunk,'failed','%s: %s'%(type(exc).__name__,str(exc)[:200])))
    else:
        for chunk in chunks:
            results.append(record_for(chunk,'failed','AUTHORIZATION_LOST: '+revoked))
    # 3) Re-check at delivery, then withhold every fact if access was lost at any point.
    if revoked is None:
        revoked=check()
    if revoked:
        for record in results:
            record.pop('facts',None)
    counts={}
    for record in results:
        counts[record['status']]=counts.get(record['status'],0)+1
    # "Report generated" is not "review complete": a manifest-only run, a failed chunk, an
    # omitted chunk or an unreadable/oversize/over-limit file all leave the root not fully
    # reviewed. A profile entry that simply is not present in this root (MISSING) is
    # reported but does not block, because there is no content there to review.
    blocking=[s for s in skipped if s.get('reason')!='MISSING']
    readable=len(chunks)
    valid_empty=sum(1 for record in results
                    if record['status']=='processed' and not record.get('facts'))
    facts=[fact for record in results for fact in (record.get('facts') or [])
           if isinstance(fact,dict)]
    evidence_linked=sum(1 for fact in facts if fact.get('evidence_ids'))
    coverage={'should_process':readable,'processed':counts.get('processed',0),
              'omitted':counts.get('omitted',0),'failed':counts.get('failed',0),
              'manifest':counts.get('manifest',0),'valid_empty':valid_empty,
              'no_readable':readable==0}
    # `complete` is a PROCESS result only: extractor ran on every readable chunk with no
    # failures/omissions and no revoked authorization. It is not a fact-accuracy verdict.
    complete=(extractor is not None and readable>0 and counts.get('failed',0)==0
              and counts.get('omitted',0)==0 and not blocking and revoked is None)
    return {'root':root,'profile_docs':names,'skipped':skipped,
            'chunks':results,'complete':complete,'revoked':revoked,
            'facts_withheld':bool(revoked),
            'coverage':coverage,
            'evidence':{'facts':len(facts),'with_evidence_ids':evidence_linked},
            'quality':'not_evaluated',
            'summary':{'chunks':len(results),'by_status':counts,'skipped':len(skipped)}}
