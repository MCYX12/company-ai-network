// Runs only in the desktop's bundled Electron/Node runtime. Never evaluates !js.
const fs = require('node:fs'), path = require('node:path');
const app = path.dirname(process.execPath);
const yaml = require(path.join(app, 'resources', 'app.asar', 'dsh', 'node_modules', 'js-yaml'));
const schema = yaml.DEFAULT_SCHEMA.extend([new yaml.Type('tag:yaml.org,2002:js', {
  kind: 'scalar', construct: () => '[expression]'
})]);
const dangerous = row => typeof row.id === 'string' && (
  row.id.startsWith('tool-') ||
  /dsh-tool-|\/tools$|dsh-mcp-client|dsh-agent-instructions|dsh-skill-filesystem|dsh-plan-mode|dsh-subagent/.test(row.name || '')
);
try {
  if (process.argv[2] === 'prepare') {
    const home = process.argv[3];
    if (fs.existsSync(home)) throw Error('Existing home');
    const source = path.join(process.env.USERPROFILE, '.dsh');
    const rows = yaml.load(fs.readFileSync(path.join(source, 'profiles', 'desktop', 'cordis.patch.yml'), 'utf8'), {schema});
    const selected = rows.filter(r => r.id === 'agent-default-model' || r.id?.startsWith('llm-'));
    if (!selected.some(r => r.id === 'agent-default-model')) throw Error('Missing selection');
    const defaults = yaml.load(fs.readFileSync(path.join(app, 'resources', 'app.asar', 'dsh', 'node_modules', '@deepseek-ai', 'dsh-base', 'cordis.patch.yml'), 'utf8'), {schema})[0].insert;
    const clean = selected.concat(defaults.filter(dangerous).map(r => ({id: r.id, disabled: true})));
    fs.mkdirSync(path.join(home, 'profiles', 'headless'), {recursive: true, mode: 0o700});
    // Local copy only; no sessions, workspace cache or desktop profile is copied.
    fs.copyFileSync(path.join(source, '.credentials.yaml'), path.join(home, '.credentials.yaml'));
    fs.writeFileSync(path.join(home, 'company-model.patch.yml'), yaml.dump(clean), {mode: 0o600});
    console.log(JSON.stringify({prepared: true}));
  } else if (process.argv[2] === 'verify') {
    const config = yaml.load(fs.readFileSync(0, 'utf8'), {schema}), rows = [];
    function walk(value) {
      if (Array.isArray(value)) value.forEach(walk);
      else if (value && typeof value === 'object') {
        if (typeof value.id === 'string' && typeof value.name === 'string') rows.push(value);
        Object.values(value).filter(x => typeof x === 'object').forEach(walk);
      }
    }
    walk(config);
    const active = rows.filter(r => dangerous(r) && r.disabled !== true).map(r => r.id);
    const selection = rows.filter(r => r.id === 'agent-default-model').map(r => ({provider: r.config?.provider, model: r.config?.model}));
    if (!rows.length || selection.length !== 1 || !selection[0].provider || !selection[0].model || active.length) throw Error('Invalid profile');
    console.log(JSON.stringify({rows: rows.length, selection, active_tool_rows: active}));
  } else throw Error('Invalid operation');
} catch (_) {
  console.error('HARNESS_PROFILE_FAILED');
  process.exitCode = 1;
}
