"""Render reviewable user service definitions; never install or start them implicitly."""
import argparse
from pathlib import Path
import plistlib


def unit_quote(value):
    value=str(value)
    if any(ord(c)<32 for c in value):raise ValueError('Control characters in service path')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'


def render(platform,root,cloud_tests=False,account_home=None):
    root=Path(root)
    if not root.is_absolute():raise ValueError('Absolute deployment path required')
    unit_quote(root)
    if platform=='ubuntu':
        if cloud_tests:raise ValueError('Server service must not enable cloud tests')
        definitions={}
        for name,command in [('stack',[str(root/'.venv/bin/python'),'-u',str(root/'scripts/run_lan.py')]),('dashboard',['/bin/bash',str(root/'scripts/start_dashboard.sh')])]:
            definitions[f'company-agent-{name}.service']=('''[Unit]
Description=Company Agent %s (user service)
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=%s
ExecStart=%s
Restart=on-failure
RestartSec=5
# Must exceed the per-child drain grace (POC_STOP_GRACE, default 20s each) so a
# maintenance stop is not cut off before the workers finish their in-flight answer.
TimeoutStopSec=150
# SIGTERM only the supervisor, which then drains its children in order; a direct
# group signal would kill an in-flight harness child before the worker can drain it.
# The whole group is still SIGKILLed if the stop timeout expires.
KillMode=mixed
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
'''%(name,str(root).replace('%','%%'),' '.join(map(unit_quote,command)))).encode()
        definitions['company-agent-healthcheck.service']=('''[Unit]
Description=Company Agent periodic health check (user service)
After=company-agent-stack.service

[Service]
Type=oneshot
WorkingDirectory=%s
ExecStart=%s
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,['/bin/bash',str(root/'scripts/run_healthcheck.sh')])))).encode()
        definitions['company-agent-healthcheck.timer']=('''[Unit]
Description=Run the Company Agent health check periodically

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
Unit=company-agent-healthcheck.service

[Install]
WantedBy=timers.target
''').encode()
        definitions['company-agent-record-refresh.service']=('''[Unit]
Description=Company Agent project-record refresh (user service)
After=company-agent-stack.service

[Service]
Type=oneshot
WorkingDirectory=%s
ExecStart=%s
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,['/bin/bash',str(root/'scripts/run_record_refresh.sh')])))).encode()
        definitions['company-agent-record-refresh.timer']=('''[Unit]
Description=Re-derive the published project record every 3h (publishes only when facts changed)

[Timer]
OnBootSec=10min
OnUnitActiveSec=3h
Persistent=true
Unit=company-agent-record-refresh.service

[Install]
WantedBy=timers.target
''').encode()
        definitions['company-agent-retention.service']=('''[Unit]
Description=Company Agent data retention cleanup (user service)

[Service]
Type=oneshot
WorkingDirectory=%s
EnvironmentFile=-%%h/.config/company-agent/operations.env
ExecStart=%s
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,[str(root/'.venv/bin/python'),'-m','scripts.retention','--runtime',str(root/'.runtime-lan'),'--apply'])))).encode()
        definitions['company-agent-retention.timer']=('''[Unit]
Description=Run the Company Agent data retention cleanup daily

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=30m
Unit=company-agent-retention.service

[Install]
WantedBy=timers.target
''').encode()
        definitions['company-agent-ocsp.service']=('''[Unit]
Description=Company Agent OCSP responder (user service)
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=%s
ExecStart=%s
Restart=always
RestartSec=2
UMask=0077
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,['/bin/bash',str(root/'scripts/run_ocsp_responder.sh')])))).encode()
        definitions['company-agent-renew.service']=('''[Unit]
Description=Company Agent certificate renewal check (user service)

[Service]
Type=oneshot
WorkingDirectory=%s
ExecStart=%s
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,['/bin/bash',str(root/'scripts/run_auto_renew.sh')])))).encode()
        definitions['company-agent-renew.timer']=('''[Unit]
Description=Daily Company Agent certificate renewal check

[Timer]
OnBootSec=10min
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1h
Unit=company-agent-renew.service

[Install]
WantedBy=timers.target
''').encode()
        definitions['company-agent-ocsp-watchdog.service']=('''[Unit]
Description=Restart the OCSP responder when it stops answering (user service)

[Service]
Type=oneshot
WorkingDirectory=%s
ExecStart=%s
'''%(str(root).replace('%','%%'),' '.join(map(unit_quote,['/bin/bash',str(root/'scripts/ocsp_watchdog.sh')])))).encode()
        definitions['company-agent-ocsp-watchdog.timer']=('''[Unit]
Description=Minute OCSP responder watchdog

[Timer]
OnBootSec=1min
OnUnitActiveSec=60
Unit=company-agent-ocsp-watchdog.service

[Install]
WantedBy=timers.target
''').encode()
        return definitions
    if platform!='mac':raise ValueError('Unknown platform')
    definitions={}
    for name,script in [('device','start-device.sh'),('dashboard','start-dashboard.sh')]:
        label='com.companyagent.'+name
        env={'PATH':str(Path(account_home or Path.home())/'.local/bin')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'}
        # CLI tools belong to the logged-in account; do not copy their credentials.
        if name=='device':env['ALLOW_CLOUD_MODEL_TESTS']='1' if cloud_tests else '0'
        definitions[label+'.plist']=plistlib.dumps({'Label':label,'ProgramArguments':['/bin/bash',str(root/script)],'WorkingDirectory':str(root),'EnvironmentVariables':env,'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ExitTimeOut':25,'Umask':0o077,'StandardOutPath':str(root/'.runtime'/('service-'+name+'.log')),'StandardErrorPath':str(root/'.runtime'/('service-'+name+'.log'))})
    definitions['com.companyagent.healthcheck.plist']=plistlib.dumps({'Label':'com.companyagent.healthcheck','ProgramArguments':['/bin/bash',str(root/'scripts/run_healthcheck.sh')],'WorkingDirectory':str(root),'EnvironmentVariables':{'PATH':str(Path(account_home or Path.home())/'.local/bin')+':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','POC_RUNTIME':str(root/'.runtime'),'HEALTH_IDENTITY':'ag-b','PYTHONUNBUFFERED':'1'},'RunAtLoad':True,'StartInterval':300,'ExitTimeOut':25,'Umask':0o077,'StandardOutPath':str(root/'.runtime'/'healthcheck.log'),'StandardErrorPath':str(root/'.runtime'/'healthcheck.log')})
    return definitions


def main():
    p=argparse.ArgumentParser();p.add_argument('--platform',choices=['ubuntu','mac'],required=True);p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cloud-tests',action='store_true')
    a=p.parse_args();definitions=render(a.platform,a.root,a.cloud_tests)
    a.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    for name,content in definitions.items():
        path=a.output/name;path.write_bytes(content);path.chmod(0o600);print(path)
if __name__=='__main__':main()
