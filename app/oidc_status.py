"""Validate the diagnostic sync snapshot, separately from authoritative IAM state."""

STATUSES={'ok','failed','partial','no_input','verified','skipped','empty'}
COUNTERS=('tokens','applied','verified','skipped','failed')


def timestamp(value, optional=False):
    return optional and value is None or type(value) in (int,float) and 0<=value<=253402300799


def valid(value):
    if not isinstance(value,dict) or value.get('status') not in tuple(STATUSES):
        return False
    if not timestamp(value.get('checked_at')):
        return False
    if any(type(value.get(key,0)) is not int or value.get(key,0)<0 for key in COUNTERS):
        return False
    if any(not timestamp(value.get(key),optional=True) for key in ('last_success','last_verify')):
        return False
    return value.get('last_error') is None or isinstance(value['last_error'],str)


def invalid():
    return {'status':'failed','failed':1,'applied':0,'skipped':0,'checked_at':None,
            'last_success':None,'last_error':'OIDC_SNAPSHOT_INVALID'}
