"""On-demand native Desktop status, opt-in and strictly metadata-only."""
import json, math, os, subprocess, sys, time
from pathlib import Path
from .client_material import independent
from .worker_kernel import process_options, terminate_process_tree

SOURCE = 'deepseek_harness_desktop_api'
ERROR_HINTS={'DESKTOP_VERSION_UNSUPPORTED','DESKTOP_AUTH_UNAVAILABLE','DESKTOP_AUTH_REJECTED',
             'DESKTOP_RESPONSE_INVALID','DESKTOP_SCHEDULE_UNAVAILABLE','DESKTOP_RESPONSE_LIMIT','DESKTOP_STATUS_TIMEOUT',
             'DESKTOP_STATUS_UNAVAILABLE'}

def unavailable(hint):
    return {'status': 'unknown', 'source': SOURCE, 'hint': hint, 'progress': None}

def validate(value, now=None):
    now = time.time() if now is None else now
    keys = {'schema_version', 'status', 'observed_at', 'source', 'scope',
            'live_sessions', 'executing_count', 'progress', 'history_read'}
    version = value.get('schema_version') if isinstance(value, dict) else None
    if version == 2:
        keys |= {'background_jobs', 'scheduled_tasks'}
    if (not isinstance(value, dict) or set(value) != keys or type(value['schema_version']) is not int
        or value['schema_version'] not in (1, 2) or value['status'] not in {'idle', 'running'}
        or value['source'] != SOURCE or value['scope'] != 'desktop_visible_sessions'
        or value['progress'] is not None or value['history_read'] is not False
        or type(value['live_sessions']) is not int or type(value['executing_count']) is not int
        or not 0 <= value['executing_count'] <= value['live_sessions'] <= 2000
        or (value['status'] == 'running') != (value['executing_count'] > 0)
        or isinstance(value['observed_at'], bool) or not isinstance(value['observed_at'], (float, int))
        or not math.isfinite(value['observed_at']) or not now - 10 <= value['observed_at'] <= now + 3):
        raise ValueError('Invalid live status')
    if version == 2:
        for name, counters in (('background_jobs', {'executing_count','total_count'}),
                               ('scheduled_tasks', {'active_count','due_count','total_count'})):
            item = value[name]
            if not isinstance(item, dict) or item.get('source') != SOURCE:
                raise ValueError('Invalid extended status')
            if item.get('status') == 'unknown':
                if set(item) != {'status','source','hint'} or item['hint'] not in ERROR_HINTS:
                    raise ValueError('Invalid extended error')
            else:
                if (set(item) != {'status','source','observed_at'} | counters
                    or any(type(item[k]) is not int or not 0 <= item[k] <= 10000 for k in counters)
                    or type(item['observed_at']) not in (float,int) or not math.isfinite(item['observed_at'])
                    or not now-10 <= item['observed_at'] <= now+3):
                    raise ValueError('Invalid extended counters')
                count = item['executing_count'] if name == 'background_jobs' else item['active_count']
                active = 'running' if name == 'background_jobs' else 'scheduled'
                if (item['status'] != (active if count else 'idle') or count > item['total_count']
                    or name == 'scheduled_tasks' and item['due_count'] > count):
                    raise ValueError('Invalid extended state')
    return {**value, 'age_seconds': max(0, now-value['observed_at']),
            'summary': 'Harness 桌面会话执行中。' if value['status']=='running' else 'Harness 桌面当前没有正在执行的模型会话。',
            'limits': ('覆盖桌面会话、后台作业和 Harness 提醒计划的数量；不含工作正文、聊天历史或项目完成百分比。'
                       if version == 2 else '只反映会话执行状态；不含后台 Shell 作业、计划任务、工作正文或项目完成百分比。')}

def desktop_status(config, root):
    entry = config.get('native_work')
    if not isinstance(entry, dict) or entry.get('enabled') is not True:
        from .client_history import native_status
        return native_status(root/'.runtime')
    if sys.platform != 'win32':
        return unavailable('DESKTOP_PLATFORM_UNSUPPORTED')
    try:
        if set(entry) != {'enabled', 'app', 'home'} or not all(isinstance(entry[k], str) for k in ('app','home')):
            raise ValueError('Configuration')
        app, home = Path(entry['app']), Path(entry['home'])
        if not app.is_absolute() or not home.is_absolute(): raise ValueError('Absolute paths required')
        exe = app/'DeepSeek Harness.exe'; helper = root/'dsh_desktop_status.cjs'
        independent(exe); independent(helper); independent(home, True)
        env = {k: v for k, v in os.environ.items() if k in
               ('SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','LOCALAPPDATA','APPDATA')}
        env['ELECTRON_RUN_AS_NODE'] = '1'
        # No caller input, model command, shell, copied credential, or cached idle state.
        with subprocess.Popen([str(exe), '--expose-internals', str(helper), str(home)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                              cwd=root, **process_options()) as proc:
            try: output, _ = proc.communicate(timeout=7)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc); proc.communicate()
                return unavailable('DESKTOP_STATUS_TIMEOUT')
            if len(output) > 4096:return unavailable('DESKTOP_STATUS_UNAVAILABLE')
            if proc.returncode:
                try:
                    error=json.loads(output)
                    if (isinstance(error,dict) and set(error)=={'status','source','hint','progress'}
                        and error['status']=='unknown' and error['source']==SOURCE and error['progress'] is None
                        and error['hint'] in ERROR_HINTS):return unavailable(error['hint'])
                except (ValueError,TypeError):pass
                return unavailable('DESKTOP_STATUS_UNAVAILABLE')
        return validate(json.loads(output))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return unavailable('DESKTOP_STATUS_INVALID')
