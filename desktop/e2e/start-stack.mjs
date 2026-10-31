// Launcher for the Playwright E2E stack.
//
// Boots the REAL Python Local API backend against a FRESH temp workspace, waits
// until it announces its port and answers `/api/health` to the owner token it
// wrote, optionally PRE-SEEDS the workspace into a configured project + active
// member via the real API, and then prints `[e2e:<stack>] ready`, which is what
// playwright.config.ts waits for. The backend serves the screens itself, from
// the Vite build in `desktop/dist` that `npm run e2e` makes first; a spec opens
// them as the owner does, through an entry link (`e2e/stack-context.ts`).
//
// Stacks launched in parallel by playwright.config.ts:
//   * "setup"     — empty workspace, no seeding, so the app lands on first-setup
//                   (journey ①, `setup.spec.ts`).
//   * "configured"— seeded workspace (project + active member), so the app boots
//                   already-configured for the service / commands journeys
//                   (journeys ③ ④, `service.spec.ts` / `commands.spec.ts`).
//   * "members"   — seeded workspace dedicated to the member-add journey
//                   (journey ②, `members.spec.ts`). Isolated from "configured"
//                   so mutating the member list never perturbs journeys ③ ④.
//   * "diagnostics"— seeded workspace for the readiness/scenario diagnostics
//                   journey (journey ⑤, `diagnostics.spec.ts`).
//   * "down"      — a backend the spec takes DOWN and brings back UP through a
//                   small control HTTP server (journey ⑥, `failure.spec.ts`), so
//                   loaded screen → unreachable → retry is deterministic rather
//                   than timing-flaky.
//   * "environment"— seeded workspace given the ready isolated agent environment
//                   of the workspace GUILDBOTICS_E2E_ENVIRONMENT_FROM names, so
//                   a command runs in a real microVM (journey ⑪,
//                   `environment.spec.ts`). The one stack that uses something
//                   of the developer's device: its runtime and that snapshot.
//
// Each stack owns its OWN temp workspace, HOME (and so its own owner token),
// ports and stack-context file, so the journeys stay fully isolated and the first-setup spec always sees
// an empty workspace. All of them live under the OS temp dir (the "environment"
// stack's under `~/.guildbotics-e2e`, see below), so a run leaves nothing behind
// inside the repository even when it is interrupted. The backend
// cwd is the temp workspace, so `/api/config/init` writes
// `<workspace>/.guildbotics/config/...` on disk. HOME is redirected to a second
// temp dir, and the backend's PATH starts with a stub bin dir, to keep the run
// hermetic.
//
// The backend runs as long as this process; when Playwright tears the web server
// down it kills this process group, and the SIGINT/SIGTERM handlers below stop
// the backend so no orphan uvicorn survives.

import { execFileSync, spawn } from "node:child_process";
import { createServer } from "node:http";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { basename, delimiter, dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { environmentValue, withEnvironment, withoutEnvironment } from "./environment.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const desktopDir = resolve(here, "..");
const repoRoot = resolve(desktopDir, "..");

// Each stack is parameterized through env vars so playwright.config.ts can launch
// several isolated instances in parallel.
const stackName = process.env.GUILDBOTICS_E2E_STACK ?? "setup";
// The server listens on loopback only.
const host = "127.0.0.1";
const backendPort = Number(process.env.GUILDBOTICS_E2E_BACKEND_PORT ?? "8766");
const shouldSeed = process.env.GUILDBOTICS_E2E_SEED === "1";
// Seed with an empty OpenAI API key so the diagnostics journey can verify the
// missing-key short-circuit WITHOUT firing a live LLM call. Keeps `npm run e2e`
// fully offline and deterministic (no dependency on api.openai.com latency or
// availability).
const seedWithoutLlmKey = process.env.GUILDBOTICS_E2E_OFFLINE_LLM === "1";
// The "down" stack's spec stops and starts the backend through the control
// server on GUILDBOTICS_E2E_CONTROL_PORT.
const controlPort = Number(process.env.GUILDBOTICS_E2E_CONTROL_PORT ?? "0");
// A workspace whose isolated agent environment is ready on this device; only
// the "environment" stack is given one (see below).
const environmentSource = process.env.GUILDBOTICS_E2E_ADOPT_ENVIRONMENT ?? "";
const baseUrl = `http://${host}:${backendPort}`;
const tag = `[e2e:${stackName}]`;

// The screens the backend serves (`guildbotics/app_api/ui.py`). They are built
// once per run by `npm run e2e`, not per stack, since the stacks start at once.
if (!existsSync(join(desktopDir, "dist", "index.html"))) {
  throw new Error(`${tag} desktop/dist is not built; run the journeys with \`npm run e2e\`.`);
}

// The stack name is interpolated into every path below, one of which is removed
// on shutdown. Keep it to an allowlist so a typo such as `../x` cannot write —
// and later delete — something outside the temp directory.
if (!/^[a-z][a-z0-9-]*$/.test(stackName)) {
  throw new Error(
    `${tag} GUILDBOTICS_E2E_STACK must match /^[a-z][a-z0-9-]*$/, got "${stackName}"`,
  );
}

// Isolated, repeatable run dirs. The isolated agent environment cannot mount a
// directory in the OS temp dir on Windows (its runtime fails to boot with "file
// already exists"), so the "environment" stack keeps its dirs under the
// developer's home instead, and clears what its previous run left there. What
// that holds of the device -- the runtime and the snapshot -- is linked, and a
// recursive removal unlinks a link without following it.
const runRoot = environmentSource ? join(homedir(), ".guildbotics-e2e") : tmpdir();
if (environmentSource) {
  rmSync(runRoot, { recursive: true, force: true });
  mkdirSync(runRoot, { recursive: true, mode: 0o700 });
}
const workspaceDir = mkdtempSync(join(runRoot, `guildbotics-e2e-${stackName}-ws-`));
const homeDir = mkdtempSync(join(runRoot, `guildbotics-e2e-${stackName}-home-`));
const configDir = join(workspaceDir, ".guildbotics", "config");
const stackEnv = withEnvironment(process.env, {
  HOME: homeDir,
  USERPROFILE: homeDir,
  PATH: environmentValue(process.env, "PATH"),
});
// Written by the backend before it announces its port: whoever can read it is
// the owner (`guildbotics/app_api/auth.py`). `e2e/stack-context.ts` derives the
// same path on the spec side.
const ownerTokenPath = join(
  homeDir,
  ".guildbotics",
  "data",
  "server",
  String(backendPort),
  "owner.token",
);

// Two facts live on the Python side and are read from there so no second copy
// has to be kept in sync: the catalog of AI CLI tools shadowed below, and where
// this stack's backend will keep the agent environment runtime. The latter is
// derived from HOME, so it is asked with the stack's temp HOME in place.
function backendFacts() {
  const source = [
    "import json",
    "from guildbotics.environment.runtime import runtime_home",
    "from guildbotics.intelligences.cli_agents import CLI_AGENTS",
    "print(json.dumps({" +
      "'executables': [agent.executable for agent in CLI_AGENTS], " +
      "'runtime_home': str(runtime_home())}))",
  ].join("; ");
  return JSON.parse(
    execFileSync("uv", ["run", "--project", repoRoot, "python", "-c", source], {
      encoding: "utf-8",
      env: stackEnv,
    }),
  );
}
const { executables: cliAgentExecutables, runtime_home: runtimeHome } = backendFacts();

// The isolated agent environment of `environmentSource`, a workspace whose
// environment is ready on this device, as the Python side reads it there (with
// the developer's own HOME): why it cannot run a command, where the device's
// runtime lives, and the snapshot it would boot.
function sourceEnvironment() {
  const source = [
    "import json",
    "from guildbotics.environment.runtime import runtime_home",
    "from guildbotics.environment.status import device_status",
    "status = device_status()",
    "print(json.dumps({'refusal': status.refusal, " +
      "'runtime_home': str(runtime_home()), " +
      "'snapshot': str(status.snapshot.path) if status.snapshot else ''}))",
  ].join("; ");
  const output = execFileSync("uv", ["run", "--project", repoRoot, "python", "-c", source], {
    encoding: "utf-8",
    env: withEnvironment(process.env, {
      GUILDBOTICS_WORKSPACE_ROOT: environmentSource,
      GUILDBOTICS_CONFIG_DIR: join(environmentSource, ".guildbotics", "config"),
    }),
  });
  return JSON.parse(output.trim().split("\n").at(-1));
}

// Every command boots inside the isolated agent environment, and the service
// keeps that environment built while it runs (`SnapshotUpkeep`). Left alone, a
// stack would copy the bundled runtime into its temp HOME and pull a container
// image over the network the moment `service.spec.ts` starts the service.
//
// A device that cannot hold the runtime is a state the product handles
// fail-closed — no build, no command, one warning in the log — so every stack
// but "environment" is made exactly that device: a plain file sits where the
// runtime home would be, and placing the runtime fails at its first mkdir. The
// SDK is still imported and version-checked; nothing is written or fetched.
//
// The "environment" stack is the device the developer runs it on instead: its
// runtime home is linked to the device's own, and the snapshot the source
// workspace is ready with is linked into the stack's workspace (after seeding,
// below), so a command runs in a real microVM without building anything.
const environment = environmentSource ? sourceEnvironment() : null;
if (environment?.refusal) {
  throw new Error(
    `${tag} ${environmentSource} cannot run a command in its isolated environment: ` +
      environment.refusal,
  );
}
mkdirSync(dirname(runtimeHome), { recursive: true, mode: 0o700 });
if (environment) {
  symlinkSync(environment.runtime_home, runtimeHome, "junction");
} else {
  writeFileSync(runtimeHome, "", { mode: 0o600 });
}

// Shadow every AI CLI tool with a stub that records the call and fails at once,
// and put the stub dir at the FRONT of the backend's PATH. No turn runs a
// provider CLI on the host: every turn boots inside the isolated agent
// environment, which this device cannot hold, so the turn is refused before a
// process starts. The stub stays as a tripwire for a journey that reaches the
// agent path — `brain: agent`, as `functions/troubleshoot` does: its log
// staying empty is how a spec proves that whatever real binary the developer
// has installed (a live, billed agent turn on a logged-in machine) was never
// launched.
const cliStubDir = mkdtempSync(join(runRoot, `guildbotics-e2e-${stackName}-bin-`));
const cliStubLog = join(cliStubDir, "invocations.log");
writeFileSync(cliStubLog, "", { mode: 0o600 });
for (const executable of cliAgentExecutables) {
  writeFileSync(
    join(cliStubDir, executable),
    [
      "#!/bin/sh",
      `echo "${executable} $*" >> "${cliStubLog}"`,
      `echo "e2e: '${executable}' is stubbed; this stack never runs a real AI CLI tool" >&2`,
      "exit 1",
      "",
    ].join("\n"),
    { mode: 0o700 },
  );
}

// Publish the run context so specs can read the on-disk project.yml / seeded ids.
// It describes this run's live processes only, so it lives beside the temp dirs
// it points at instead of inside the repo. `e2e/stack-context.ts` mirrors this
// path derivation on the reader side; the stack name is the only shared input.
// The context names this run's temp HOME, which holds its owner token, and on
// Linux the temp dir is shared between local users, so both the directory and
// the file must be user-private. `mkdirSync` / `writeFileSync` apply `mode` only when they
// create, hence the explicit chmod for a directory left by an earlier run and
// the removal of any stale file before writing.
const contextDir = join(tmpdir(), "guildbotics-e2e");
const contextPath = join(contextDir, `${stackName}.json`);
const seededMemberId = "local-agent";
mkdirSync(contextDir, { recursive: true, mode: 0o700 });
chmodSync(contextDir, 0o700);
rmSync(contextPath, { force: true });
writeFileSync(
  contextPath,
  JSON.stringify(
    {
      stackName,
      workspaceDir,
      homeDir,
      configDir,
      cliStubLog,
      backendPort,
      controlPort,
      host,
      seeded: shouldSeed,
      seededWithoutLlmKey: shouldSeed && seedWithoutLlmKey,
      memberId: shouldSeed ? seededMemberId : null,
    },
    null,
    2,
  ),
  { mode: 0o600 },
);
console.log(`${tag} stack context: ${contextPath}`);

let backendEnv = withEnvironment(stackEnv, {
  HOME: homeDir,
  USERPROFILE: homeDir,
  PATH: [cliStubDir, environmentValue(stackEnv, "PATH")].filter(Boolean).join(delimiter),
  GUILDBOTICS_SERVER_PORT: String(backendPort),
});
// `withEnvironment` replaces a Windows parent environment's `Path` key instead
// of leaving two spellings.
// The backend never derives a workspace from its cwd, so the stack's temp
// workspace must be selected explicitly; remove any inherited config override.
backendEnv = withoutEnvironment(backendEnv, ["GUILDBOTICS_CONFIG_DIR"]);
backendEnv.GUILDBOTICS_WORKSPACE_ROOT = workspaceDir;
// Keep e2e secrets in a throwaway file-backed keyring stub instead of
// polluting the developer's OS keychain (or failing on keychain-less CI).
writeFileSync(
  join(cliStubDir, "guildbotics_e2e_keyring.py"),
  [
    '"""File-backed keyring stub so e2e stacks never touch a real OS keychain."""',
    "import json",
    "import os",
    "",
    "from keyring.backend import KeyringBackend",
    "from keyring.errors import PasswordDeleteError",
    "",
    "",
    "class E2EKeyring(KeyringBackend):",
    "    priority = 100",
    "",
    "    def _path(self):",
    '        return os.path.join(os.environ["HOME"], "e2e-keyring.json")',
    "",
    "    def _load(self):",
    "        try:",
    '            with open(self._path(), encoding="utf-8") as handle:',
    "                return json.load(handle)",
    "        except (OSError, ValueError):",
    "            return {}",
    "",
    "    def _save(self, data):",
    '        with open(self._path(), "w", encoding="utf-8") as handle:',
    "            json.dump(data, handle)",
    "",
    "    def get_password(self, service, username):",
    "        return self._load().get(service, {}).get(username)",
    "",
    "    def set_password(self, service, username, password):",
    "        data = self._load()",
    "        data.setdefault(service, {})[username] = password",
    "        self._save(data)",
    "",
    "    def delete_password(self, service, username):",
    "        data = self._load()",
    "        try:",
    "            del data[service][username]",
    "        except KeyError as exc:",
    "            raise PasswordDeleteError(username) from exc",
    "        self._save(data)",
    "",
  ].join("\n"),
  { mode: 0o600 },
);
backendEnv.PYTHON_KEYRING_BACKEND = "guildbotics_e2e_keyring.E2EKeyring";
backendEnv.PYTHONPATH = [cliStubDir, environmentValue(backendEnv, "PYTHONPATH")]
  .filter(Boolean)
  .join(delimiter);
// Offline-LLM stacks must NOT inherit a developer's real LLM key from the
// shell; otherwise the diagnostics missing-key short-circuit cannot be
// asserted deterministically.
if (seedWithoutLlmKey) {
  backendEnv = withoutEnvironment(backendEnv, [
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
  ]);
}

let backend;
let controlServer;
let shuttingDown = false;

// Start the backend, and settle once it answers: first it announces, on its
// own stdout, that it holds the port, and only then is the owner token at
// `ownerTokenPath` this server's own, safe to send to whatever listens there.
async function startBackend() {
  const child = spawn("uv", ["run", "--project", repoRoot, "python", "-m", "guildbotics.app_api"], {
    cwd: workspaceDir,
    env: backendEnv,
    stdio: ["ignore", "pipe", "inherit"],
  });
  backend = child;
  child.on("exit", (code) => {
    if (backend === child && !shuttingDown) {
      console.error(`${tag} backend exited early with code ${code ?? "null"}`);
      shutdown(1);
    }
  });
  const notice = `GUILDBOTICS_APP_API_PORT=${backendPort}`;
  await new Promise((resolve, reject) => {
    createInterface({ input: child.stdout }).on("line", (line) =>
      line === notice ? resolve() : console.log(line),
    );
    child.on("exit", () => reject(new Error(`${tag} backend exited before it held the port`)));
  });
  await waitForHealth();
  console.log(`${tag} backend healthy at ${baseUrl} (workspace=${workspaceDir})`);
}

// Stop the backend as the spec asks, without taking the stack down with it.
async function stopBackend() {
  const child = backend;
  backend = undefined;
  if (child && child.exitCode === null) {
    await new Promise((resolve) => {
      child.on("exit", resolve);
      child.kill("SIGTERM");
    });
  }
  console.log(`${tag} backend stopped`);
}

function shutdown(code) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  // Best effort: a SIGKILLed process group skips this, which is why the context
  // path is derived rather than unique — the next run of the same stack
  // overwrites the file instead of piling up a new one.
  rmSync(contextPath, { force: true });
  if (controlServer) {
    controlServer.close();
  }
  if (backend && backend.exitCode === null) {
    backend.kill("SIGTERM");
  }
  if (typeof code === "number") {
    setTimeout(() => process.exit(code), 200);
  }
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));
process.on("exit", () => shutdown());

function ownerHeaders() {
  const token = readFileSync(ownerTokenPath, "utf-8").trim();
  return { Authorization: `Bearer ${token}`, "Content-Type": "application/json" };
}

async function waitForHealth() {
  const deadline = Date.now() + 60_000;
  let lastError = "unknown";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/api/health`, { headers: ownerHeaders() });
      if (response.ok) {
        return;
      }
      lastError = `status ${response.status}`;
    } catch (error) {
      lastError = String(error);
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`${tag} backend did not become healthy: ${lastError}`);
}

async function postJson(path, body) {
  const response = await fetch(`${baseUrl}/api${path}`, {
    method: "POST",
    headers: ownerHeaders(),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(
      `${tag} POST ${path} failed: status ${response.status} ${await response.text()}`,
    );
  }
  return response.json();
}

// Seed a configured project + one active member directly through the real API,
// mirroring how `tests/guildbotics/app_api/test_api_integration.py` provisions a
// workspace. This makes the app boot on `/service` and `/commands` already wired
// to a member, and the empty-workspace seeding also creates the sample commands
// (e.g. `context-info`) used by journey ④.
async function seedWorkspace() {
  await postJson("/config/init", {
    config_dir: configDir,
    language: "en",
    description: "E2E configured workspace",
    llm_api_type: "openai",
    cli_agent: "codex",
    openai_api_key: seedWithoutLlmKey ? "" : "sk-e2e-test-key",
    // The reference provider keeps every kind of service in the workspace, so
    // nothing reaches a hosted service.
    services: {
      code_hosting_service: { name: "local", owner: "acme" },
      ticket_service: { name: "local", owner: "acme" },
      chat_service: { name: "local" },
    },
  });
  await postJson("/config/members", {
    config_dir: configDir,
    person_type: "",
    person_id: seededMemberId,
    person_name: "Local Agent",
    is_active: true,
    git_email: "",
    roles: ["architect"],
    speaking_style: "concise",
  });
  // The seeded `cli_agent` never runs on the host: a journey that does reach
  // the agent path is refused by this device's agent environment before a
  // process starts, and the stub in `cliStubDir` proves no host binary ran.
  // The command journey (`brain: none`) and the service journey (event
  // trigger only) stay off that path to begin with.
  console.log(`${tag} seeded configured workspace (workspace=${workspaceDir})`);
}

// Give the "environment" stack's workspace the source workspace's declaration
// and the snapshot the source is ready with, so the stack's environment is
// ready without a build. The snapshot is linked rather than copied (it is a
// multi-gigabyte disk image), and only that one: nothing in the journey builds
// (the service's upkeep does, and this stack's journey never starts it).
function adoptSourceEnvironment() {
  const declaration = join("intelligences", "agent_environment.yml");
  mkdirSync(dirname(join(configDir, declaration)), { recursive: true });
  copyFileSync(
    join(environmentSource, ".guildbotics", "config", declaration),
    join(configDir, declaration),
  );
  const snapshots = join(workspaceDir, ".guildbotics", "local", "agent_environment", "snapshots");
  mkdirSync(snapshots, { recursive: true });
  symlinkSync(environment.snapshot, join(snapshots, basename(environment.snapshot)), "junction");
  console.log(`${tag} adopted the isolated environment of ${environmentSource}`);
}

// Control server for the "down" journey (⑥): the spec takes the REAL backend
// down under a loaded screen, and brings it back up. Each answer comes only
// once the backend is gone, or answers again, so the spec's next step (the
// screen noticing the loss, a retry) cannot race the backend.
function startControlServer() {
  const actions = {
    "/control/stop-backend": stopBackend,
    "/control/start-backend": () => (backend ? Promise.resolve() : startBackend()),
  };
  let busy = false;
  controlServer = createServer((req, res) => {
    const reply = (status, body) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(body));
    };
    const action = req.method === "POST" ? actions[req.url] : undefined;
    if (!action) {
      reply(404, { status: "not-found" });
      return;
    }
    if (busy) {
      reply(409, { status: "busy" });
      return;
    }
    busy = true;
    action()
      .then(
        () => reply(200, { status: "done" }),
        (error) => reply(500, { status: "error", error: String(error) }),
      )
      .finally(() => {
        busy = false;
      });
  });
  return new Promise((resolve) =>
    controlServer.listen(controlPort, host, () => {
      console.log(`${tag} control server listening on http://${host}:${controlPort}`);
      resolve();
    }),
  );
}

async function main() {
  await startBackend();
  if (shouldSeed) {
    await seedWorkspace();
  }
  if (environment) {
    adoptSourceEnvironment();
  }
  if (controlPort) {
    await startControlServer();
  }
  console.log(`${tag} ready at ${baseUrl}`);
}

main().catch((error) => {
  console.error(error);
  shutdown(1);
});
