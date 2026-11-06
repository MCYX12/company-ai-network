"""Fail-closed production admission: bind field evidence and accountable sign-off to source.

This checks evidence completeness, scope, age and integrity, not truth of field assertions.
No local unit test, service heartbeat or drill can auto-complete a field item.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
ITEMS={
    'E-01':('identity','Real IdP and join/move/leave lifecycle'),
    'E-02':('platform','Physical HA, shared authority and partitions'),
    'E-03':('model','Real model and offline cold start'),
    'E-04':('business','Authorized business data and independent quality review'),
    'E-05':('network','VLAN/VPN, addressing, certificates and clock'),
    'E-06':('endpoint','Platform install/upgrade/rollback matrix'),
    'E-07':('platform','1000 connections, 100 read-only QPS, 30 minutes'),
    'E-08':('operations','Real on-call delivery, acknowledgment and escalation'),
    'E-09':('security','Independent immutable audit retention'),
    'E-10':('operations','Encrypted off-site full restore and production RTO/RPO'),
    'E-11':('business','50-200 employee pilot and business/security/ops sign-offs'),
    'E-12':('security','SLO, permissions, retention, supported versions and ownership'),
}


def source_digest(root=ROOT):
    from scripts.source_integrity import hashes
    return hashlib.sha256(json.dumps(hashes(root),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def template():
    return {'format':1,'scope':'production','source_digest':None,
            'environment':{'kind':'field','site':None},
            'items':{key:{'description':value[1],'owner':None,'responsible_role':value[0],
                          'status':'pending','evidence_level':'现场验收','accepted_at':None,
                          'evidence':[], 'metrics':{}} for key,value in ITEMS.items()},
            'signoffs':{'business':None,'security':None,'operations':None},
            'note':'Populate only from real field evidence and named responsible people; template is deliberately non-passing.'}


def verify(path, root=ROOT, now=None, max_age_days=30):
    path=Path(path);problems=[]
    now=now or datetime.now(timezone.utc)
    try:data=json.loads(path.read_text())
    except (OSError,ValueError) as error:return {'ok':False,'problems':['acceptance manifest unavailable: '+str(error)]}
    if not isinstance(data,dict):return {'ok':False,'problems':['acceptance manifest must be an object']}
    if type(data.get('format')) is not int or data.get('format')!=1 or data.get('scope')!='production':problems.append('production acceptance format/scope required')
    if data.get('source_digest')!=source_digest(root):problems.append('acceptance source digest missing or stale')
    def named(value):return isinstance(value,str) and 1<=len(value.strip())<=200
    environment=data.get('environment',{})
    if not isinstance(environment,dict) or environment.get('kind')!='field' or not named(environment.get('site')):problems.append('named field environment required')
    entries=data.get('items',{})
    if not isinstance(entries,dict):entries={};problems.append('items must be an object')
    def fresh(value):
        if not isinstance(value,str):return False
        try:
            accepted=datetime.fromisoformat(value.replace('Z','+00:00'))
            return accepted.tzinfo is not None and 0<=(now-accepted).total_seconds()<=max_age_days*86400
        except (ValueError,TypeError,OverflowError):return False
    for key in ITEMS:
        row=entries.get(key) or {}
        if not isinstance(row,dict):problems.append(key+': invalid item');continue
        if row.get('status')!='accepted' or row.get('evidence_level')!='现场验收' or not named(row.get('owner')) or row.get('responsible_role')!=ITEMS[key][0]:
            problems.append(key+': named owner and field acceptance required')
        try:
            accepted=datetime.fromisoformat(row['accepted_at'].replace('Z','+00:00'))
            age=(now-accepted).total_seconds()
            if age<0 or age>max_age_days*86400:raise ValueError('expired/future acceptance')
        except (KeyError,TypeError,ValueError,AttributeError):problems.append(key+': valid fresh acceptance time required')
        evidence=row.get('evidence')
        if not isinstance(evidence,list) or not evidence:
            problems.append(key+': evidence required');continue
        for entry in evidence:
            try:
                if not isinstance(entry,dict) or not isinstance(entry.get('path'),str):raise ValueError('invalid evidence descriptor')
                relative=Path(entry['path'])
                file=path.parent/relative
                if relative.is_absolute() or '..' in relative.parts or file.is_symlink() or not file.resolve().is_relative_to(path.parent.resolve()):raise ValueError('evidence must stay inside acceptance directory')
                actual=hashlib.sha256(file.read_bytes()).hexdigest()
                if actual!=entry.get('sha256'):raise ValueError('evidence checksum mismatch')
            except (OSError,TypeError,ValueError) as error:problems.append(key+': '+str(error))
    # Required business conditions are checked as explicit measured values, not just a PASS label.
    def metrics(key):
        row=entries.get(key) or {};value=row.get('metrics') if isinstance(row,dict) else None
        return value if isinstance(value,dict) else {}
    def numeric(value):return isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<float('inf')
    def count(value):return type(value) is int and value>=0
    ha=metrics('E-02')
    if not count(ha.get('physical_failure_domains')) or not count(ha.get('routers')) or ha.get('physical_failure_domains',0)<3 or ha.get('routers',0)<2 or ha.get('partition_revocation_passed') is not True:
        problems.append('E-02: three physical failure domains, two Routers and partition/revocation proof required')
    offline=metrics('E-03')
    if offline.get('real_model') is not True or offline.get('public_network_blocked') is not True or offline.get('cold_start_passed') is not True:
        problems.append('E-03: real model offline cold-start measurements required')
    capacity=metrics('E-07')
    if not count(capacity.get('connections')) or not all(numeric(capacity.get(key)) for key in ('qps','duration_seconds','success_ratio','p95_ms')) or not (capacity['connections']>=1000 and capacity['qps']>=100 and capacity['duration_seconds']>=1800 and .999<=capacity['success_ratio']<=1 and capacity['p95_ms']<=500):
        problems.append('E-07: measured enterprise capacity baseline not met')
    alert=metrics('E-08')
    if not named(alert.get('receiver_receipt')) or alert.get('real_channel') is not True:problems.append('E-08: independent receipt from real channel required')
    worm=metrics('E-09')
    if worm.get('independent_storage') is not True or worm.get('immutable_retention') is not True:problems.append('E-09: independent immutable storage evidence required')
    dr=metrics('E-10')
    if not all(numeric(dr.get(key)) for key in ('rto_seconds','rpo_seconds','rto_target_seconds','rpo_target_seconds')) or not (dr['rto_seconds']<=dr['rto_target_seconds'] and dr['rpo_seconds']<=dr['rpo_target_seconds']):
        problems.append('E-10: measured production RTO/RPO and targets required')
    if any(dr.get(key) is not True for key in ('encrypted','offsite','complete_restore')):problems.append('E-10: encrypted off-site complete restore evidence required')
    pilot=metrics('E-11')
    if not count(pilot.get('employees')) or not 50<=pilot['employees']<=200:problems.append('E-11: 50-200 employee pilot required')
    signoffs=data.get('signoffs')
    if not isinstance(signoffs,dict) or any(not isinstance(signoffs.get(key),dict) or not named(signoffs[key].get('name')) or not fresh(signoffs[key].get('accepted_at')) for key in ('business','security','operations')):
        problems.append('business/security/operations named and fresh sign-offs required')
    return {'ok':not problems,'problems':problems,'required_items':list(ITEMS),
            'scope':'field evidence admission; does not independently attest facts or signatures'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path)
    parser.add_argument('--template',type=Path)
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    if args.template:
        if args.template.exists():raise ValueError('Never overwrite an existing acceptance record')
        args.template.write_text(json.dumps(template(),ensure_ascii=False,indent=2)+'\n');return 0
    if not args.manifest:parser.error('--manifest or --template required')
    result=verify(args.manifest,args.root)
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
