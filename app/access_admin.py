"""Run only on the trusted Router host as its service administrator."""
import argparse,json
from .conversation_access import AccessStore

def build_parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['list','approve','deny','revoke'])
    p.add_argument('--id');p.add_argument('--seconds',type=int,default=0,help='validity in seconds; 0 = no expiry (default)');p.add_argument('--reason',default='')
    return p

def main():
    a=build_parser().parse_args();store=AccessStore()
    if a.action!='list':store.decide(a.id,{'approve':'approved','deny':'denied','revoke':'revoked'}[a.action],a.seconds,a.reason)
    print(json.dumps(store.listing(),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
