"""Create an explicit local export from a user-selected JSON message list.

Input: [{"role":"assistant","text":"...","timestamp":<Unix seconds>}, ...].
This utility never discovers desktop storage, enables access or uploads messages.
"""
import argparse,json,math,re,time
from pathlib import Path

def export(source,destination,project):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',project):raise ValueError('Project ID')
    if source.stat().st_size>1024*1024:raise ValueError('Input too large')
    rows=json.loads(source.read_text(encoding='utf-8-sig'))
    if not isinstance(rows,list) or len(rows)>3999:raise ValueError('Message limit')
    out=[json.dumps({'schema':'company_harness_history_v1','project_id':project})]
    for row in rows:
        if (not isinstance(row,dict) or set(row)!={'role','text','timestamp'} or row['role'] not in {'user','assistant'}
            or not isinstance(row['text'],str) or len(row['text'])>20000 or isinstance(row['timestamp'],bool)
            or not isinstance(row['timestamp'],(int,float)) or not math.isfinite(row['timestamp'])
            or not 0<=row['timestamp']<=time.time()+300):raise ValueError('Message schema')
        out.append(json.dumps(row,ensure_ascii=False))
    raw=('\n'.join(out)+'\n').encode()
    if len(raw)>1024*1024:raise ValueError('Export too large')
    with destination.open('xb') as stream:stream.write(raw)
    return {'export':str(destination),'messages':len(rows),'enabled':False,'uploaded':False}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--project',required=True)
    a=p.parse_args();print(json.dumps(export(a.input,a.output,a.project)))
