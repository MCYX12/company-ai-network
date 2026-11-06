// Fixed, read-only Desktop RPC. Credentials and full list rows never leave this process.
const fs = require('node:fs'), path = require('node:path');
const http = require('node:http'), crypto = require('node:crypto');
const AUTHORITY = '127.0.0.1:19387', MAX_BYTES = 4 * 1024 * 1024;
const HINTS = new Set(['DESKTOP_VERSION_UNSUPPORTED','DESKTOP_AUTH_UNAVAILABLE','DESKTOP_AUTH_REJECTED',
  'DESKTOP_RESPONSE_INVALID','DESKTOP_SCHEDULE_UNAVAILABLE','DESKTOP_RESPONSE_LIMIT','DESKTOP_STATUS_TIMEOUT','DESKTOP_STATUS_UNAVAILABLE']);
function failure(code) { const e = Error('Desktop status unavailable'); e.code = code; return e; }
function errorHint(error) { return HINTS.has(error?.code) ? error.code : 'DESKTOP_STATUS_UNAVAILABLE'; }
function independent(file, directory = false) {
  for (let p = file; ; p = path.dirname(p)) {
    const s = fs.lstatSync(p);
    if (s.isSymbolicLink() || (p === file && (directory ? !s.isDirectory() : !s.isFile() || s.nlink !== 1))) throw Error('Unsafe path');
    if (path.dirname(p) === p) break;
  }
}
function cookie(record, now = Date.now()) {
  if (record?.kind !== 'grant' || record.payload?.version !== 1 ||
      typeof record.payload.secret !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(record.payload.secret)) throw Error('Missing browser credential');
  const secret = Buffer.from(record.payload.secret, 'base64url');
  if (secret.length !== 32 || secret.toString('base64url') !== record.payload.secret) throw Error('Invalid browser credential');
  // Backdate one second for Windows inter-process wall-clock quantization; total lifetime stays 20s.
  const body = Buffer.from(JSON.stringify({version: 1, authority: AUTHORITY, issuedAt: now - 1000, expiresAt: now + 19000})).toString('base64url');
  const signature = crypto.createHmac('sha256', secret).update(body).digest('base64url');
  const name = crypto.createHash('sha256').update(AUTHORITY).digest('base64url');
  return `dsh-auth-${name}=v1.${body}.${signature}`;
}
function summarize(value, now = Date.now()) {
  if (!value || !Array.isArray(value.items) || value.items.length > 2000) throw Error('Invalid list');
  let live = 0, running = 0; const ids = new Set();
  for (const row of value.items) {
    if (!row || typeof row.sessionId !== 'string' || !row.sessionId || ids.has(row.sessionId) ||
        typeof row.agentAvailable !== 'boolean' || typeof row.running !== 'boolean' ||
        row.running && !row.agentAvailable) throw Error('Invalid row');
    ids.add(row.sessionId);
    if (row.agentAvailable) { live++; if (row.running) running++; }
  }
  return {schema_version: 1, status: running ? 'running' : 'idle', observed_at: now / 1000,
    source: 'deepseek_harness_desktop_api', scope: 'desktop_visible_sessions',
    live_sessions: live, executing_count: running, progress: null, history_read: false};
}
function rpc(auth, method, request = {}) {
  if (!['session/list','schedule/catalog'].includes(method)) throw Error('Read-only endpoint required');
  return new Promise((resolve, reject) => {
    const rpcId = crypto.randomUUID();
    const payload = JSON.stringify({type: 'client-request', rpcId, method, payload: {args: method==='session/list'?{_request:request}:{}}});
    const req = http.request({hostname: '127.0.0.1', port: 19387, path: '/api/'+method, method: 'POST',
      headers: {'content-type': 'application/json', 'content-length': Buffer.byteLength(payload), cookie: auth}}, res => {
      if (res.statusCode !== 200) { res.resume(); reject(failure(res.statusCode===404&&method==='schedule/catalog'?'DESKTOP_SCHEDULE_UNAVAILABLE':[401,403].includes(res.statusCode) ? 'DESKTOP_AUTH_REJECTED' : 'DESKTOP_STATUS_UNAVAILABLE')); return; }
      let bytes = 0; const parts = [];
      res.on('data', b => { bytes += b.length; if (bytes > MAX_BYTES) req.destroy(failure('DESKTOP_RESPONSE_LIMIT')); else parts.push(b); });
      res.on('error', reject);
      res.on('end', () => {
        try {
          const envelope = JSON.parse(Buffer.concat(parts).toString('utf8'));
          if (envelope.type !== 'server-response' || envelope.rpcId !== rpcId || envelope.result?.ok !== true) throw Error('Invalid RPC');
          clearTimeout(deadline);resolve(envelope.result.value);
        } catch { reject(failure('DESKTOP_RESPONSE_INVALID')); }
      });
    });
    const deadline=setTimeout(()=>req.destroy(failure('DESKTOP_STATUS_TIMEOUT')),2500);
    req.on('close',()=>clearTimeout(deadline));req.on('error',reject);req.end(payload);
  });
}
async function list(auth) { return summarize(await rpc(auth,'session/list')); }
function observation(status, values, now = Date.now()) {
  return {status, source:'deepseek_harness_desktop_api', observed_at:now/1000, ...values};
}
function unknown(error) { return {status:'unknown',source:'deepseek_harness_desktop_api',hint:errorHint(error)}; }
function jobsSummary(frames, now = Date.now()) {
  const jobs=new Map();
  for(const frame of frames){
    if(frame?.type!=='rows'||!Array.isArray(frame.jobs)||frame.jobs.length>2000)throw failure('DESKTOP_RESPONSE_INVALID');
    for(const row of frame.jobs){
      if(!row||typeof row.id!=='string'||!row.id||!['running','stopping','completed','killed','failed'].includes(row.status))throw failure('DESKTOP_RESPONSE_INVALID');
      if(jobs.has(row.id)&&jobs.get(row.id)!==row.status)throw failure('DESKTOP_RESPONSE_INVALID');
      jobs.set(row.id,row.status);
    }
  }
  if(jobs.size>10000)throw failure('DESKTOP_RESPONSE_LIMIT');
  const executing_count=[...jobs.values()].filter(s=>['running','stopping'].includes(s)).length;
  return observation(executing_count?'running':'idle',{executing_count,total_count:jobs.size},now);
}
function schedulesSummary(rows, now = Date.now()) {
  if(!Array.isArray(rows)||rows.length>10000)throw failure('DESKTOP_RESPONSE_INVALID');
  let active_count=0,due_count=0;const ids=new Set();
  for(const row of rows){
    if(!row||typeof row.id!=='string'||!row.id||ids.has(row.id)||!['active','inactive'].includes(row.status)||
       typeof row.scheduledAt!=='string'||!Number.isFinite(Date.parse(row.scheduledAt)))throw failure('DESKTOP_RESPONSE_INVALID');
    ids.add(row.id);if(row.status==='active'){active_count++;if(Date.parse(row.scheduledAt)<=now)due_count++;}
  }
  return observation(active_count?'scheduled':'idle',{active_count,due_count,total_count:rows.length},now);
}
function jobs(auth, sessions, WebSocket) {
  if(sessions.length>64)return Promise.reject(failure('DESKTOP_RESPONSE_LIMIT'));
  if(!sessions.length)return Promise.resolve(jobsSummary([]));
  return new Promise((resolve,reject)=>{
    const pending=new Map(sessions.map(session=>[crypto.randomUUID(),session.sessionId]));
    const frames=[];let settled=false,bytes=0;
    const ws=new WebSocket('ws://127.0.0.1:19387/api/remote.mux',{headers:{cookie:auth},maxPayload:MAX_BYTES,handshakeTimeout:2500,followRedirects:false});
    const finish=(error)=>{if(settled)return;settled=true;clearTimeout(timer);ws.terminate();
      if(error)reject(error);else{try{resolve(jobsSummary(frames));}catch(e){reject(e);}}};
    const timer=setTimeout(()=>finish(failure('DESKTOP_STATUS_TIMEOUT')),2500);
    ws.on('open',()=>{for(const [streamId,sessionId]of pending)ws.send(JSON.stringify({type:'open',streamId,endpoint:'job/list',payload:{args:{request:{sessionId}}}}));});
    ws.on('message',(data,isBinary)=>{
      try{
        bytes+=data.length;if(isBinary||bytes>MAX_BYTES)throw failure('DESKTOP_RESPONSE_LIMIT');
        const frame=JSON.parse(data.toString('utf8'));
        if(!pending.has(frame.streamId))return;
        if(frame.type!=='item')throw failure('DESKTOP_RESPONSE_INVALID');
        frames.push(frame.value);pending.delete(frame.streamId);
        ws.send(JSON.stringify({type:'cancel',streamId:frame.streamId}));
        if(!pending.size)finish();
      }catch(e){finish(e);}
    });
    ws.on('unexpected-response',(_req,res)=>{res.resume();finish(failure([401,403].includes(res.statusCode)?'DESKTOP_AUTH_REJECTED':'DESKTOP_STATUS_UNAVAILABLE'));});
    ws.on('error',()=>finish(failure('DESKTOP_STATUS_UNAVAILABLE')));
    ws.on('close',()=>{if(!settled)finish(failure('DESKTOP_STATUS_UNAVAILABLE'));});
  });
}
async function fullStatus(auth, WebSocket) {
  const [sessions,schedules]=await Promise.all([rpc(auth,'session/list'),rpc(auth,'schedule/catalog').then(schedulesSummary).catch(unknown)]);
  const summary=summarize(sessions);
  return {...summary,schema_version:2,background_jobs:await jobs(auth,sessions.items,WebSocket).catch(unknown),scheduled_tasks:schedules};
}
async function main(home) {
  if (!path.isAbsolute(home)) throw Error('Absolute home required');
  const base = path.join(path.dirname(process.execPath), 'resources', 'app.asar', 'dsh', 'node_modules');
  const version = JSON.parse(fs.readFileSync(path.join(base, '@deepseek-ai', 'dsh-api-session-controller', 'package.json'), 'utf8')).version;
  if (version !== '0.2.0-rc.2') throw failure('DESKTOP_VERSION_UNSUPPORTED');
  let auth;
  try {
    const file = path.join(home, '.credentials.yaml'); independent(file);
    if (fs.statSync(file).size > 1024 * 1024) throw Error('Credential size limit');
    const yaml = require(path.join(base, 'yaml'));
    const document = yaml.parseDocument(fs.readFileSync(file, 'utf8'), {uniqueKeys: true});
    if (document.errors.length || document.get('version') !== 1) throw Error('Credential format');
    // Read only the browser-session record, never export provider keys or account grants.
    auth = cookie(document.getIn(['records', 'client-connection/browser-session'])?.toJSON());
  } catch (error) { throw failure('DESKTOP_AUTH_UNAVAILABLE'); }
  return fullStatus(auth, require(path.join(base,'ws')));
}
module.exports = {cookie, summarize, independent, list, main, errorHint, rpc, jobsSummary, schedulesSummary, jobs, fullStatus};
if (require.main === module) main(process.argv[2]).then(v => console.log(JSON.stringify(v))).catch(error => {
  console.log(JSON.stringify({status: 'unknown', source: 'deepseek_harness_desktop_api',
    hint: errorHint(error), progress: null})); process.exitCode = 1;
});
