"""Chunk authorized profile documents so answers cite sections, not just a file head.

Only a Router-approved root and the versioned profile allowlist reach these functions. A
document is split on Markdown headings first, then by a hard character cap on line
boundaries; a single line longer than the cap is itself split so no chunk can exceed it.
Every chunk keeps its file, line range, heading and a content hash. Reaching the chunk cap
is reported as an explicit drop count, never silently swallowed.
"""
import hashlib
import re

# Policy, not a silent cap: a document above this is reported as skipped with a reason
# instead of being dropped without trace.
MAX_DOC_BYTES = 262144
CHUNK_CHARS = 4000
MAX_CHUNKS_PER_DOC = 400

_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_ASCII_TERM = re.compile(r'[a-z0-9_]{2,}')
_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')


def terms(text):
    """Question/document vocabulary: ASCII words plus CJK bi-grams (no jieba needed)."""
    text=(text or '').casefold()
    found=set(_ASCII_TERM.findall(text))
    for run in _CJK_RUN.findall(text):
        if len(run)==1:
            found.add(run)
        for i in range(len(run)-1):
            found.add(run[i:i+2])
    return found


def chunk_sha(text):
    return hashlib.sha256((text or '').encode('utf-8','replace')).hexdigest()


def _split_all(text,max_chars=CHUNK_CHARS):
    """Every chunk of the document, before any cap. No chunk exceeds max_chars."""
    lines=(text or '').splitlines()
    max_chars=max(200,int(max_chars))
    sections=[];heading='';start=0
    for i,line in enumerate(lines):
        match=_HEADING.match(line)
        if match:
            if i>start:sections.append((heading,start,i))
            heading=match.group(2).strip()[:120]
            start=i
    sections.append((heading,start,len(lines)))
    chunks=[]
    for heading,begin,end in sections:
        block='\n'.join(lines[begin:end]).strip('\n')
        if not block.strip():
            continue
        if len(block)<=max_chars:
            chunks.append({'heading':heading,'start_line':begin+1,'end_line':end,'text':block})
            continue
        buf=[];buf_start=begin;size=0
        for idx in range(begin,end):
            line=lines[idx]
            if len(line)>max_chars:
                # A single over-long line must be split too, or it would exceed the cap.
                if buf:
                    chunks.append({'heading':heading,'start_line':buf_start+1,'end_line':idx,'text':'\n'.join(buf)})
                    buf=[];size=0
                for offset in range(0,len(line),max_chars):
                    chunks.append({'heading':heading,'start_line':idx+1,'end_line':idx+1,
                                   'text':line[offset:offset+max_chars]})
                buf_start=idx+1
                continue
            if buf and size+len(line)+1>max_chars:
                chunks.append({'heading':heading,'start_line':buf_start+1,'end_line':idx,'text':'\n'.join(buf)})
                buf=[];buf_start=idx;size=0
            buf.append(line);size+=len(line)+1
        if buf:
            chunks.append({'heading':heading,'start_line':buf_start+1,'end_line':end,'text':'\n'.join(buf)})
    return chunks


def split_chunks(text,max_chars=CHUNK_CHARS,limit=MAX_CHUNKS_PER_DOC):
    """Chunks capped at `limit`; see split_chunks_with_drops to learn what was dropped."""
    return _split_all(text,max_chars)[:limit]


def split_chunks_with_drops(text,max_chars=CHUNK_CHARS,limit=MAX_CHUNKS_PER_DOC):
    """Return (kept_chunks, dropped_count) so a capped document cannot look complete."""
    chunks=_split_all(text,max_chars)
    return chunks[:limit], max(0,len(chunks)-limit)


def scored_chunks(chunks, question):
    """Rank chunks for one question; heading matches weigh more than body matches."""
    wanted=terms(question)
    if not wanted:
        return [(0,i,chunk) for i,chunk in enumerate(chunks)]
    scored=[]
    for i,chunk in enumerate(chunks):
        head=len(wanted & terms(chunk.get('heading')))
        body=len(wanted & terms((chunk.get('text') or '')[:2000]))
        scored.append((head*3+body,i,chunk))
    return scored


def rank_chunks(chunks, question, limit=None):
    scored=scored_chunks(chunks,question)
    scored.sort(key=lambda item:(-item[0],item[1]))
    ranked=[chunk for _score,_i,chunk in scored]
    return ranked[:limit] if limit else ranked
