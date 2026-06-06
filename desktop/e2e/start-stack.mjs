// Launcher for the Playwright E2E stack.
//
// Boots the REAL Python Local API backend against a FRESH temp workspace, waits
// for `/health`, optionally PRE-SEEDS the workspace into a configured project +
// active member via the real API, then starts Vite in browser-preview mode wired
// to that backend via VITE_GUILDBOTICS_API_TOKEN / VITE_GUILDBOTICS_API_BASE.
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
//   * "down"      — DEFERRED backend: the frontend boots pointed at a backend
//                   port that is NOT yet serving (journey ⑥, `failure.spec.ts`).
//                   A small control HTTP server lets the spec bring the real
//                   backend UP on demand, so backend-down → up is deterministic
//                   rather than timing-flaky.
//
// Each stack owns its OWN temp workspace, HOME, ports, token and stack-context
// file, so the journeys stay fully isolated and the first-setup spec always sees
// an empty workspace. The backend cwd is the temp workspace, so `/config/init`
// writes `<workspace>/.guildbotics/config/...` on disk. HOME is redirected to a
// second temp dir to keep the run hermetic.
//
// Vite runs in the foreground; when Playwright tears the web server down it kills
// this process group, and the SIGINT/SIGTERM handlers below stop the backend so
// no orphan uvicorn survives.

import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const desktopDir = resolve(here, "..");
const repoRoot = resolve(desktopDir, "..");

// Each stack is parameterized through env vars so playwright.config.ts can launch
// several isolated instances in parallel.
const stackName = process.env.GUILDBOTICS_E2E_STACK ?? "setup";
const host = process.env.GUILDBOTICS_E2E_HOST ?? "127.0.0.1";
const backendPort = Number(process.env.GUILDBOTICS_E2E_BACKEND_PORT ?? "8766");
const frontendPort = Number(process.env.GUILDBOTICS_E2E_FRONTEND_PORT ?? "1421");
const token = process.env.GUILDBOTICS_E2E_TOKEN ?? "e2e-token";
const shouldSeed = process.env.GUILDBOTICS_E2E_SEED === "1";
// "down" mode does not start the backend at boot; the spec brings it up later
// through the control server on GUILDBOTICS_E2E_CONTROL_PORT.
const deferBackend = process.env.GUILDBOTICS_E2E_DEFER_BACKEND === "1";
const controlPort = Number(process.env.GUILDBOTICS_E2E_CONTROL_PORT ?? "0");
const contextFile = process.env.GUILDBOTICS_E2E_CONTEXT_FILE ?? ".stack-context.json";
const baseUrl = `http://${host}:${backendPort}`;
const authHeaders = { "X-GuildBotics-Session-Token": token, "Content-Type": "application/json" };
const tag = `[e2e:${stackName}]`;

// Isolated, repeatable run dirs.
const workspaceDir = mkdtempSync(join(tmpdir(), `guildbotics-e2e-${stackName}-ws-`));
const homeDir = mkdtempSync(join(tmpdir(), `guildbotics-e2e-${stackName}-home-`));
const configDir = join(workspaceDir, ".guildbotics", "config");
const envFile = join(workspaceDir, ".env");

// Publish the run context so specs can read the on-disk project.yml / seeded ids.
const seededMemberId = "local-agent";
writeFileSync(
  join(desktopDir, "e2e", contextFile),
  JSON.stringify(
    {
      stackName,
      workspaceDir,
      homeDir,
      configDir,
      envFile,
      backendPort,
      frontendPort,
      controlPort,
      token,
      host,
      seeded: shouldSeed,
      deferBackend,
      memberId: shouldSeed ? seededMemberId : null,
    },
    null,
    2,
  ),
);

const backendEnv = { ...process.env, HOME: homeDir };
// Force the workspace config layout (`<cwd>/.guildbotics/config`) by removing any
// inherited override; this yields config-status `primary_config_location=workspace`.
delete backendEnv.GUILDBOTICS_CONFIG_DIR;

let backend;
let frontend;
let controlServer;
let shuttingDown = false;

function spawnBackend() {
  return spawn(
    "uv",
    [
      "run",
      "--project",
      repoRoot,
      "python",
      "-m",
      "guildbotics.app_api",
      "--host",
      host,
      "--port",
      String(backendPort),
      "--token",
      token,
    ],
    { cwd: workspaceDir, env: backendEnv, stdio: "inherit" },
  );
}

function attachBackendExitGuard() {
  backend.on("exit", (code) => {
    if (!shuttingDown) {
      console.error(`${tag} backend exited early with code ${code ?? "null"}`);
      shutdown(1);
    }
  });
}

function shutdown(code) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  if (controlServer) {
    controlServer.close();
  }
  if (frontend && frontend.exitCode === null) {
    frontend.kill("SIGTERM");
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

async function waitForHealth() {
  const deadline = Date.now() + 60_000;
  let lastError = "unknown";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseUrl}/health`, { headers: authHeaders });
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
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: authHeaders,
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
    env_file_path: envFile,
    env_file_option: "overwrite",
    language: "en",
    description: "E2E configured workspace",
    llm_api_type: "openai",
    cli_agent: "codex",
    openai_api_key: "sk-e2e-test-key",
  });
  await postJson("/config/members", {
    config_dir: configDir,
    env_file_path: envFile,
    person_type: "",
    person_id: seededMemberId,
    person_name: "Local Agent",
    is_active: true,
    github_username: "",
    git_email: "",
    roles: ["architect"],
    speaking_style: "concise",
  });
  console.log(`${tag} seeded configured workspace (workspace=${workspaceDir})`);
}

function startFrontend() {
  frontend = spawn(
    "npm",
    ["run", "dev", "--", "--host", host, "--port", String(frontendPort), "--strictPort"],
    {
      cwd: desktopDir,
      env: {
        ...process.env,
        VITE_GUILDBOTICS_API_TOKEN: token,
        VITE_GUILDBOTICS_API_BASE: baseUrl,
      },
      stdio: "inherit",
    },
  );

  frontend.on("exit", (code) => {
    shutdown(code ?? 0);
  });
}

// Control server for the "down" journey (⑥). The frontend is started pointed at a
// backend port that is NOT yet serving, so the app boots into Bootstrap's error
// state. The spec then POSTs `/control/start-backend` here; we start the REAL
// backend, wait for `/health`, and only then answer 200 — so the subsequent
// retry click in the app is guaranteed to find a healthy backend (no race).
function startControlServer() {
  let backendStarting = false;
  controlServer = createServer((req, res) => {
    if (req.method === "POST" && req.url === "/control/start-backend") {
      if (backend) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "already-running" }));
        return;
      }
      if (backendStarting) {
        res.writeHead(409, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ status: "starting" }));
        return;
      }
      backendStarting = true;
      console.log(`${tag} control: starting backend on demand`);
      backend = spawnBackend();
      attachBackendExitGuard();
      waitForHealth()
        .then(() => {
          console.log(`${tag} control: backend healthy at ${baseUrl}`);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: "ready" }));
        })
        .catch((error) => {
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ status: "error", error: String(error) }));
        });
      return;
    }
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "not-found" }));
  });
  controlServer.listen(controlPort, host, () => {
    console.log(`${tag} control server listening on http://${host}:${controlPort}`);
  });
}

async function main() {
  if (deferBackend) {
    // Backend stays DOWN until the spec asks the control server to start it.
    startControlServer();
    startFrontend();
    return;
  }

  backend = spawnBackend();
  attachBackendExitGuard();
  await waitForHealth();
  console.log(`${tag} backend healthy at ${baseUrl} (workspace=${workspaceDir})`);

  if (shouldSeed) {
    await seedWorkspace();
  }

  startFrontend();
}

main().catch((error) => {
  console.error(error);
  shutdown(1);
});
