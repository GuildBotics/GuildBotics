// The macOS agent owns one loop; hub-server.sh owns each server operation.
import { spawn } from 'node:child_process';
import * as fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const hub = path.join(process.env.HOME, 'Library/Application Support/GuildBotics/Hub');
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
async function readAgent() {
  const plist = path.join(process.env.HOME, 'Library/LaunchAgents/local.guildbotics.plist');
  for (const key of ['BRANVIL_DATA_DIR', 'GUILDBOTICS_HOME', 'GUILDBOTICS_SERVER_PORT']) {
    try { process.env[key] = await run('plutil', ['-extract', `EnvironmentVariables.${key}`, 'raw', '-o', '-', plist]); }
    catch (error) { if (key !== 'GUILDBOTICS_SERVER_PORT') throw error; }
  }
}
async function notify(sha, stage, reason, next) {
  const key = `${sha}:${stage}`;
  if (state.notified?.includes(key)) return;
  const message = `Hub ${sha}: ${stage}: ${reason}. Next: ${next}.`;
  console.error(`${new Date().toISOString()} ${message}`);
  try {
    // With no webhook, the agent's stderr log is the notification destination.
    await run(current, ['server', 'hub-notify'], message);
    state.notified = [...(state.notified ?? []), key];
    save();
  } catch (error) { console.error(error.message); }
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
    if (fs.existsSync(path.join(hub, 'operation'))) {
      await notify(sha, 'operation', 'Another operation or an abandoned lock exists', 'wait; if it persists, verify that its processes ended before removing Hub/operation');
      return;
    }
    if (fs.existsSync(path.join(hub, 'builds/update-from'))) {
      await notify(sha, 'recovery', 'An interrupted update remains', 'run scripts/hub-server.sh recover');
      return;
    }
    await restoreService();
    await git('fetch', 'origin', 'main');
    sha = await git('rev-parse', '--verify', 'FETCH_HEAD^{commit}');
    const source = await run(current, ['server', 'hub-probe', '--source']);
    if (path.basename(source) === sha) return;
    if (state.attempt === sha && ['failed', 'updating'].includes(state.result)) return;
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
    if (migration.manual_update_version > installed.manual_update_version) {
      await notify(sha, stage, `Manual update required: ${migration.instructions}`, 'follow those instructions and update manually');
      return;
    }
    stage = 'image';
    const environment = await cli('environment', 'status', '--format', 'json');
    if (environment.image?.reference && environment.image.present !== true) {
      await notify(sha, stage, `Custom base image is not loaded: ${environment.image.reference}`, 'load the declared image with guildbotics environment image load');
      return;
    }
    stage = 'service';
    const service = await cli('service', 'status');
    if (!service.server_running) return;
    state.serviceBefore = service.running;
    // Persist intent before moving HEAD: a terminated update must not retry.
    state.attempt = sha;
    state.result = 'updating';
    save();
    stage = 'update';
    await git('merge', '--ff-only', sha);
    try {
      const output = await run('sh', ['scripts/hub-server.sh', 'update'], undefined,
        () => notify(sha, 'waiting', 'Update is still waiting or building', 'keep waiting; inspect the Hub log'));
      console.log(output);
      state.result = 'success';
    } catch (error) {
      state.result = 'failed';
      await notify(sha, stage, error.message, 'inspect the Hub log; recover if needed; a new commit is required for automatic retry');
    }
    save();
    if (!fs.existsSync(path.join(hub, 'builds/update-from'))) await restoreService();
    if (state.result === 'success') {
      stage = 'snapshot';
      const after = await cli('environment', 'status', '--format', 'json');
      if (['missing', 'stale', 'failed'].includes(after.snapshot?.state)) {
        await notify(sha, stage, `Snapshot is ${after.snapshot.state}`, 'run guildbotics environment build');
      }
    }
  } catch (error) {
    await notify(sha, stage, error.message, 'inspect the auto-update log and correct the cause');
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
