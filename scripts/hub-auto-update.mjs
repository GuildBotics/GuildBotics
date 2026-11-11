// The macOS agent owns one loop; hub-server.sh owns each server operation.
import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import * as fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// GUILDBOTICS_HUB_INSTANCE names a Hub copy, as in hub-server.sh.
const instance = process.env.GUILDBOTICS_HUB_INSTANCE ?? '';
const hub = path.join(process.env.HOME, `Library/Application Support/GuildBotics/Hub${instance && `-${instance}`}`);
const current = path.join(hub, 'builds/current');
const statePath = path.join(hub, 'auto-update.json');
let state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, 'utf8')) : {};
const interval = Number(process.env.GUILDBOTICS_HUB_UPDATE_INTERVAL ?? 300);
const waitNotice = Number(process.env.GUILDBOTICS_HUB_WAIT_NOTICE ?? 3600);
if (!(interval > 0 && waitNotice > 0)) throw new Error('Update intervals must be positive.');
process.umask(0o077);
function save() {
  fs.writeFileSync(`${statePath}.new`, JSON.stringify(state));
  fs.renameSync(`${statePath}.new`, statePath);
}
function run(command, args, input, onWait) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd: repo, stdio: ['pipe', 'pipe', 'inherit'] });
    let output = '';
    child.stdout.on('data', chunk => { output += chunk; });
    child.on('error', reject);
    const timer = onWait && setTimeout(() => void onWait(), waitNotice * 1000);
    child.on('close', code => {
      clearTimeout(timer);
      if (code === 0) resolve(output.trim());
      else reject(new Error(`${path.basename(command)} ${args[0]} exited ${code}`));
    });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}
const git = (...args) => run('git', ['-C', repo, ...args]);
const cli = (...args) => run(current, args).then(JSON.parse);
const agentSettings = ['GUILDBOTICS_HOME', 'BRANVIL_DATA_DIR', 'GUILDBOTICS_SERVER_PORT', 'GUILDBOTICS_IDENTITY_ROOT'];
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
// Guidance runs this Hub's build with its settings: a bare guildbotics may run
// another Hub's build against the default machine root.
const guildbotics = () => [
  ...agentSettings.filter(key => process.env[key]).map(key => `${key}=${quote(process.env[key])}`),
  quote(current),
].join(' ');
async function readAgent() {
  const plist = path.join(process.env.HOME, `Library/LaunchAgents/local.guildbotics${instance && `.${instance}`}.plist`);
  for (const key of agentSettings) {
    try { process.env[key] = await run('plutil', ['-extract', `EnvironmentVariables.${key}`, 'raw', '-o', '-', plist]); }
    catch (error) {
      if (['BRANVIL_DATA_DIR', 'GUILDBOTICS_HOME'].includes(key)) throw error;
      // Like hub-server.sh, use the agent's setting rather than the shell's.
      delete process.env[key];
    }
  }
}
async function notify(sha, stage, reason, next, key = `${sha}:${stage}`) {
  if (state.notified?.includes(key)) return;
  // A copy's notice names it: its commands need GUILDBOTICS_HUB_INSTANCE.
  const message = `Hub${instance && ` copy ${instance} (GUILDBOTICS_HUB_INSTANCE=${instance})`} ${sha}: ${stage}: ${reason}. Next: ${next}.`;
  console.error(`${new Date().toISOString()} ${message}`);
  state.pendingNotices ??= {};
  state.pendingNotices[key] = message;
  save();
  await deliver(key, message);
}
async function deliver(key, message) {
  try {
    // With no webhook, the agent's stderr log is the notification destination.
    await run(current, ['server', 'hub-notify'], message);
    state.notified = [...(state.notified ?? []), key];
    delete state.pendingNotices[key];
    save();
  } catch (error) { console.error(error.message); }
}
// State-derived failures have an episode lifetime independent of commits.
function clearBlocker(stage) {
  if (state.blockers?.[stage]) {
    delete state.blockers[stage];
    save();
  }
}
function fileIdentity(file) {
  const stat = fs.statSync(file);
  return `${stat.dev}:${stat.ino}:${stat.birthtimeMs}`;
}
async function blocker(sha, stage, reason, next, identity = reason, delay = 0) {
  state.blockers ??= {};
  if (state.blockers[stage]?.identity !== identity) {
    state.blockers[stage] = { identity, since: Date.now(), key: randomUUID() };
    save();
  }
  const episode = state.blockers[stage];
  if (Date.now() - episode.since >= delay * 1000) {
    await notify(sha, stage, reason, next, episode.key);
  }
}
function failureRecord() {
  try { return fs.readFileSync(path.join(hub, 'last-failure'), 'utf8').trim(); }
  catch (error) { if (error.code === 'ENOENT') return ''; throw error; }
}
async function restoreService() {
  if (typeof state.serviceBefore === 'boolean') {
    const service = await cli('service', 'status');
    if (!service.server_running) throw new Error('Server is unavailable; service resume remains pending');
    if (service.running !== state.serviceBefore) {
      await run(current, ['service', state.serviceBefore ? 'start' : 'stop']);
    }
    delete state.serviceBefore;
    save();
  }
}
export async function tick() {
  let sha = state.attempt ?? 'unknown';
  let stage = 'preflight';
  try {
    for (const [key, message] of Object.entries(state.pendingNotices ?? {})) await deliver(key, message);
    if (fs.existsSync(path.join(hub, 'operation'))) {
      await blocker(sha, 'operation', 'The same Hub operation lock has persisted', 'verify that its processes ended before removing Hub/operation', fileIdentity(path.join(hub, 'operation')), waitNotice);
      return;
    }
    clearBlocker('operation');
    if (fs.existsSync(path.join(hub, 'builds/update-from'))) {
      await blocker(sha, 'recovery', 'An interrupted update remains', 'run scripts/hub-server.sh recover', fileIdentity(path.join(hub, 'builds/update-from')));
      return;
    }
    clearBlocker('recovery');
    await restoreService();
    await git('fetch', 'origin', 'main');
    sha = await git('rev-parse', '--verify', 'FETCH_HEAD^{commit}');
    const source = await run(current, ['server', 'hub-probe', '--source']);
    clearBlocker('preflight');
    if (path.basename(source) === sha) return;
    if (state.attempt === sha && ['failed', 'updating'].includes(state.result)) {
      if (state.result === 'updating') await notify(sha, 'update', 'Previous automatic attempt ended without an outcome', 'inspect the Hub log; recover if needed or update manually');
      return;
    }
    if (await git('status', '--porcelain')) {
      await notify(sha, 'checkout', 'Checkout has uncommitted changes', 'commit or remove the changes');
      return;
    }
    try { await git('merge-base', '--is-ancestor', 'HEAD', sha); }
    catch {
      await notify(sha, 'checkout', 'Checkout cannot fast-forward to main', 'restore a checkout on origin/main');
      return;
    }
    stage = 'checks';
    const checks = await cli('server', 'hub-checks', '--checkout', repo, '--sha', sha);
    clearBlocker(stage);
    if (checks.state === 'failure') {
      await notify(sha, stage, 'Required checks failed', 'inspect or rerun the checks');
      return;
    }
    if (checks.state !== 'success') return;
    stage = 'migration';
    const declared = await git('ls-tree', '--name-only', sha, '--', 'hub-update.json');
    const migration = declared ? JSON.parse(await git('show', `${sha}:hub-update.json`)) : { manual_update_version: 0 };
    const installed = await cli('server', 'hub-migration');
    if (!Number.isSafeInteger(migration.manual_update_version)) throw new Error('Invalid migration declaration');
    clearBlocker(stage);
    if (migration.manual_update_version > installed.manual_update_version) {
      await notify(sha, stage, `Manual update required: ${migration.instructions}`, 'follow those instructions and update manually');
      return;
    }
    stage = 'image';
    const environment = await cli('environment', 'status', '--format', 'json');
    clearBlocker(stage);
    if (environment.image?.reference && environment.image.present !== true) {
      await notify(sha, stage, `Custom base image is not loaded: ${environment.image.reference}`, `load the declared image with ${guildbotics()} environment image load`);
      return;
    }
    stage = 'service';
    const service = await cli('service', 'status');
    if (!service.server_running) {
      await blocker(sha, stage, 'Server is not running', 'inspect the server log and run restart or recover');
      return;
    }
    clearBlocker(stage);
    state.serviceBefore = service.running;
    // Persist intent before moving HEAD: a terminated update must not retry.
    state.attempt = sha;
    state.result = 'updating';
    save();
    stage = 'update';
    await git('merge', '--ff-only', sha);
    const previousFailure = failureRecord();
    try {
      const output = await run('sh', ['scripts/hub-server.sh', 'update'], undefined,
        () => notify(sha, 'waiting', 'Update is still waiting or building', 'keep waiting; inspect the Hub log'));
      console.log(output);
      state.result = 'success';
    } catch (error) {
      state.result = 'failed';
      const failure = failureRecord();
      const detail = failure && failure !== previousFailure ? failure : error.message;
      const recovery = fs.existsSync(path.join(hub, 'builds/update-from')) ? ' Interrupted update remains; recovery required.' : '';
      await notify(sha, stage, detail + recovery, 'inspect the Hub log; recover if needed; a new commit is required for automatic retry');
    }
    save();
    if (!fs.existsSync(path.join(hub, 'builds/update-from'))) await restoreService();
    if (state.result === 'success') {
      stage = 'snapshot';
      const after = await cli('environment', 'status', '--format', 'json');
      clearBlocker(stage);
      if (['missing', 'stale', 'failed', 'building'].includes(after.snapshot?.state)) {
        await notify(sha, stage, `Snapshot is ${after.snapshot.state}`, `run ${guildbotics()} environment build (or wait for the running build), then ${guildbotics()} environment prune`);
      }
    }
  } catch (error) {
    await blocker(sha, stage, error.message, 'inspect the auto-update log and correct the cause');
  }
}

await readAgent();
if (process.argv[2] === '--once') {
  await tick();
} else {
  // Exit after a script change; launchd starts a fresh Node process and module.
  const original = fs.readFileSync(fileURLToPath(import.meta.url));
  do {
    await tick();
    if (!original.equals(fs.readFileSync(fileURLToPath(import.meta.url)))) break;
    await new Promise(resolve => setTimeout(resolve, interval * 1000));
  } while (true);
}
