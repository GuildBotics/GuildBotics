// Real Rust HTTP API and browser stacks with private temporary state.
// Each stack has an ephemeral vault and AI CLI tripwires. Its microVM runtime
// is blocked, except the "environment" stack's, which borrows this device's.

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
import { delimiter, dirname, join, resolve, sep } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { start, stop } from "./child.mjs";
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

const runRoot = tmpdir();
// The runtime opens unix sockets under HOME/.guildbotics/data/msb, and macOS
// caps their paths at 103 bytes: a stack that boots microVMs makes its run
// directory, under a short path in the user's home, its HOME, as the server
// blackbox tests do. It is removed on shutdown.
const adoptedRoot = environmentSource ? adoptionRoot() : "";
const workspaceDir = adoptedRoot
  ? join(adoptedRoot, "workspace")
  : mkdtempSync(join(runRoot, `guildbotics-e2e-${stackName}-ws-`));
const homeDir = adoptedRoot || mkdtempSync(join(runRoot, `guildbotics-e2e-${stackName}-home-`));

function adoptionRoot() {
  const parent = join(homedir(), ".gbe");
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const root = mkdtempSync(parent + sep);
  mkdirSync(join(root, "workspace"));
  return root;
}
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

const cliAgentExecutables = ["codex", "claude", "gemini"];
const runtimeHome = join(homeDir, ".guildbotics", "data", "msb");
mkdirSync(dirname(runtimeHome), { recursive: true, mode: 0o700 });
if (environmentSource) {
  // Borrow the ready environment: the declaration is copied, and the snapshots
  // and the runtime are linked, so no build or download starts.
  const declaration = join(".guildbotics", "config", "intelligences", "agent_environment.yml");
  mkdirSync(dirname(join(workspaceDir, declaration)), { recursive: true });
  copyFileSync(join(environmentSource, declaration), join(workspaceDir, declaration));
  const snapshots = join(".guildbotics", "local", "agent_environment", "snapshots");
  mkdirSync(dirname(join(workspaceDir, snapshots)), { recursive: true });
  symlinkSync(join(environmentSource, snapshots), join(workspaceDir, snapshots), "dir");
  const deviceHome = process.env.GUILDBOTICS_HOME ?? join(homedir(), ".guildbotics");
  symlinkSync(join(deviceHome, "data", "msb"), runtimeHome, "dir");
} else {
  // Refuse before a runtime or an image can be placed or fetched.
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
// The example server injects a process-local vault into the real HTTP API.
// An allowlist drops ambient credentials and host/workspace overrides.
backendEnv = Object.fromEntries(
  Object.entries(backendEnv).filter(([key]) =>
    [
      "HOME",
      "USERPROFILE",
      "PATH",
      "SYSTEMROOT",
      "WINDIR",
      "COMSPEC",
      "PATHEXT",
      "GUILDBOTICS_SERVER_PORT",
      "GUILDBOTICS_WORKSPACE_ROOT",
    ].includes(key.toUpperCase()),
  ),
);
if (stackName === "setup") {
  backendEnv.GUILDBOTICS_BROWSER_TEST_BRANVIL = "1";
  backendEnv.BRANVIL_DATA_DIR = join(homeDir, "branvil");
}
backendEnv.GUILDBOTICS_HOME = join(homeDir, ".guildbotics");
backendEnv.HTTP_PROXY = backendEnv.HTTPS_PROXY = backendEnv.ALL_PROXY = "http://127.0.0.1:1";
backendEnv.NO_PROXY = "localhost,127.0.0.1,::1";

let backend;
let controlServer;
let shuttingDown = false;

// Start the backend, and settle once it answers: first it announces, on its
// own stdout, that it holds the port, and only then is the owner token at
// `ownerTokenPath` this server's own, safe to send to whatever listens there.
async function startBackend() {
  const server = start(
    resolve(
      repoRoot,
      "target/debug/examples/browser-test-server" + (process.platform === "win32" ? ".exe" : ""),
    ),
    [],
    {
      cwd: workspaceDir,
      env: backendEnv,
      stdio: ["ignore", "pipe", "inherit"],
    },
  );
  backend = server;
  server.ended.then((reason) => {
    if (backend === server && !shuttingDown) {
      console.error(`${tag} backend ended early: ${reason}`);
      shutdown(1);
    }
  });
  const notice = `GUILDBOTICS_APP_API_PORT=${backendPort}`;
  await new Promise((resolve, reject) => {
    createInterface({ input: server.child.stdout }).on("line", (line) =>
      line === notice ? resolve() : console.log(line),
    );
    server.ended.then((reason) =>
      reject(new Error(`${tag} backend ended before it held the port: ${reason}`)),
    );
  });
  await waitForHealth();
  console.log(`${tag} backend healthy at ${baseUrl} (workspace=${workspaceDir})`);
}

// Stop the backend as the spec asks, without taking the stack down with it.
async function stopBackend() {
  const server = backend;
  backend = undefined;
  if (server) {
    await stop(server);
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
  // The run directory goes once the backend, whose microVMs use it, is gone.
  // Removing it removes the links themselves, never what they point at.
  const finish = () => {
    if (adoptedRoot) {
      rmSync(adoptedRoot, { recursive: true, force: true });
    }
    if (typeof code === "number") {
      setTimeout(() => process.exit(code), 200);
    }
  };
  if (backend) {
    stop(backend).then(finish);
  } else {
    finish();
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
      const response = await fetch(`${baseUrl}/-/guildbotics/api/health`, {
        headers: ownerHeaders(),
      });
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
  const response = await fetch(`${baseUrl}/-/guildbotics/api${path}`, {
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
    person_type: "agent",
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
  if (controlPort) {
    await startControlServer();
  }
  console.log(`${tag} ready at ${baseUrl}`);
}

main().catch((error) => {
  console.error(error);
  shutdown(1);
});
