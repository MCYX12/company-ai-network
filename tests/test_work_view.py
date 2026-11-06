"""User-visible freshness and privacy boundaries for the current-work cards."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkViewTests(unittest.TestCase):
    def test_live_stale_unknown_sources_and_untrusted_strings(self):
        script = r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const esc=source.slice(source.indexOf('const esc='),source.indexOf('\nconst statusNames='));
const date=source.slice(source.indexOf('function date('),source.indexOf('\nasync function api('));
const render=source.slice(source.indexOf('const workLabels='),source.indexOf("\n$('#agent-cards').addEventListener"));
const context=vm.createContext({Date,errors:{TIMEOUT:'目标未及时回复'}});
vm.runInContext(esc+'\n'+date+'\n'+render+'\nthis.renderWork=renderWork;',context);
const now=Date.now()/1000;
const live={native_work:{status:'running',observed_at:now,executing_count:2,source:'deepseek_harness_desktop_api'},worker_activity:{state:'result_pending',observed_at:now,executing_count:0,results_pending:1,source:'live_worker_runtime'},queried_at:now};
let html=context.renderWork(live);
for(const text of ['正在执行','执行 2 项','结果等待确认','等待确认 1 项','Harness 桌面实时接口','平台实时执行状态','执行状态不代表项目完成'])assert(html.includes(text),text);
for(const at of [now-121,now+31,undefined]){
 html=context.renderWork({...live,native_work:{...live.native_work,status:'idle',observed_at:at},worker_activity:{}});
 assert(html.includes('观察已过期'));assert(!html.includes('当前无执行任务'));
}
html=context.renderWork({native_work:{status:'not_integrated'},worker_activity:{state:'unknown'},status_query_error:'TIMEOUT'});
assert(html.includes('尚未接入'));assert(html.includes('状态未知'));assert(html.includes('目标未及时回复'));
html=context.renderWork({native_work:{status:'unknown',hint:'DESKTOP_AUTH_REJECTED',limits:'<img src=x onerror=alert(1)>'},worker_activity:{state:'<script>alert(1)</script>'},status_query_error:'<script>private</script>'});
assert(html.includes('桌面拒绝状态读取'));assert(!html.includes('<img'));assert(!html.includes('<script>'));assert(html.includes('&lt;img'));
console.log('PASS');
'''
        result = subprocess.run(['node', '-e', script, str(ROOT/'web/app.js')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS')


if __name__ == '__main__':
    unittest.main()
