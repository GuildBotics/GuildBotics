import { defineConfig, devices } from "@playwright/test";

// Real-browser E2E layer for the desktop app. Each run boots the REAL Python
// Local API backend against a fresh temp workspace and a Vite dev server wired to
// it in browser-preview mode (no Tauri). See `e2e/start-stack.mjs` for the
// harness that owns process lifecycle and the isolated workspace.
//
// Five fully isolated stacks run in parallel, each matched to its spec by
// filename via `testMatch`:
//   * "setup"       — empty workspace (first-setup journey ①, `setup.spec.ts`).
//   * "configured"  — pre-seeded project + active member (service / commands
//                     journeys ③ ④, `service.spec.ts` / `commands.spec.ts`).
//   * "members"     — pre-seeded workspace dedicated to the member-add journey
//                     (journey ②, `members.spec.ts`); isolated from "configured"
//                     so mutating the member list never perturbs ③ ④.
//   * "diagnostics" — pre-seeded workspace for the readiness / scenario
//                     diagnostics journey (journey ⑤, `diagnostics.spec.ts`).
//   * "down"        — frontend booted against a NOT-yet-serving backend; the
//                     spec brings the real backend up on demand via a control
//                     server (critical-failure journey ⑥, `failure.spec.ts`).
// Each stack owns its own backend port, frontend port, token and on-disk
// workspace, so the journeys never share state.

const HOST = process.env.GUILDBOTICS_E2E_HOST ?? "127.0.0.1";

const SETUP_BACKEND_PORT = Number(process.env.GUILDBOTICS_E2E_BACKEND_PORT ?? "8766");
const SETUP_FRONTEND_PORT = Number(process.env.GUILDBOTICS_E2E_FRONTEND_PORT ?? "1421");
const CONFIGURED_BACKEND_PORT = Number(
  process.env.GUILDBOTICS_E2E_CONFIGURED_BACKEND_PORT ?? "8767",
);
const CONFIGURED_FRONTEND_PORT = Number(
  process.env.GUILDBOTICS_E2E_CONFIGURED_FRONTEND_PORT ?? "1422",
);
const MEMBERS_BACKEND_PORT = Number(process.env.GUILDBOTICS_E2E_MEMBERS_BACKEND_PORT ?? "8768");
const MEMBERS_FRONTEND_PORT = Number(process.env.GUILDBOTICS_E2E_MEMBERS_FRONTEND_PORT ?? "1423");
const DIAGNOSTICS_BACKEND_PORT = Number(
  process.env.GUILDBOTICS_E2E_DIAGNOSTICS_BACKEND_PORT ?? "8769",
);
const DIAGNOSTICS_FRONTEND_PORT = Number(
  process.env.GUILDBOTICS_E2E_DIAGNOSTICS_FRONTEND_PORT ?? "1424",
);
const DOWN_BACKEND_PORT = Number(process.env.GUILDBOTICS_E2E_DOWN_BACKEND_PORT ?? "8770");
const DOWN_FRONTEND_PORT = Number(process.env.GUILDBOTICS_E2E_DOWN_FRONTEND_PORT ?? "1425");
const DOWN_CONTROL_PORT = Number(process.env.GUILDBOTICS_E2E_DOWN_CONTROL_PORT ?? "8771");

const SETUP_BASE_URL = `http://${HOST}:${SETUP_FRONTEND_PORT}`;
const CONFIGURED_BASE_URL = `http://${HOST}:${CONFIGURED_FRONTEND_PORT}`;
const MEMBERS_BASE_URL = `http://${HOST}:${MEMBERS_FRONTEND_PORT}`;
const DIAGNOSTICS_BASE_URL = `http://${HOST}:${DIAGNOSTICS_FRONTEND_PORT}`;
const DOWN_BASE_URL = `http://${HOST}:${DOWN_FRONTEND_PORT}`;

const SETUP_TOKEN = "e2e-token-setup";
const CONFIGURED_TOKEN = "e2e-token-configured";
const MEMBERS_TOKEN = "e2e-token-members";
const DIAGNOSTICS_TOKEN = "e2e-token-diagnostics";
const DOWN_TOKEN = "e2e-token-down";

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "line" : "list",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    headless: true,
    locale: "en-US",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "setup",
      testMatch: /setup\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: SETUP_BASE_URL },
    },
    {
      name: "configured",
      testMatch: /(service|commands)\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: CONFIGURED_BASE_URL },
    },
    {
      name: "members",
      testMatch: /members\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: MEMBERS_BASE_URL },
    },
    {
      name: "diagnostics",
      testMatch: /diagnostics\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: DIAGNOSTICS_BASE_URL },
    },
    {
      name: "down",
      testMatch: /failure\.spec\.ts$/,
      use: { ...devices["Desktop Chrome"], baseURL: DOWN_BASE_URL },
    },
  ],
  webServer: [
    {
      command: "node e2e/start-stack.mjs",
      url: `${SETUP_BASE_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        GUILDBOTICS_E2E_STACK: "setup",
        GUILDBOTICS_E2E_HOST: HOST,
        GUILDBOTICS_E2E_BACKEND_PORT: String(SETUP_BACKEND_PORT),
        GUILDBOTICS_E2E_FRONTEND_PORT: String(SETUP_FRONTEND_PORT),
        GUILDBOTICS_E2E_TOKEN: SETUP_TOKEN,
        GUILDBOTICS_E2E_CONTEXT_FILE: ".stack-context.json",
      },
    },
    {
      command: "node e2e/start-stack.mjs",
      url: `${CONFIGURED_BASE_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        GUILDBOTICS_E2E_STACK: "configured",
        GUILDBOTICS_E2E_SEED: "1",
        GUILDBOTICS_E2E_HOST: HOST,
        GUILDBOTICS_E2E_BACKEND_PORT: String(CONFIGURED_BACKEND_PORT),
        GUILDBOTICS_E2E_FRONTEND_PORT: String(CONFIGURED_FRONTEND_PORT),
        GUILDBOTICS_E2E_TOKEN: CONFIGURED_TOKEN,
        GUILDBOTICS_E2E_CONTEXT_FILE: ".stack-context-configured.json",
      },
    },
    {
      command: "node e2e/start-stack.mjs",
      url: `${MEMBERS_BASE_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        GUILDBOTICS_E2E_STACK: "members",
        GUILDBOTICS_E2E_SEED: "1",
        GUILDBOTICS_E2E_HOST: HOST,
        GUILDBOTICS_E2E_BACKEND_PORT: String(MEMBERS_BACKEND_PORT),
        GUILDBOTICS_E2E_FRONTEND_PORT: String(MEMBERS_FRONTEND_PORT),
        GUILDBOTICS_E2E_TOKEN: MEMBERS_TOKEN,
        GUILDBOTICS_E2E_CONTEXT_FILE: ".stack-context-members.json",
      },
    },
    {
      command: "node e2e/start-stack.mjs",
      url: `${DIAGNOSTICS_BASE_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        GUILDBOTICS_E2E_STACK: "diagnostics",
        GUILDBOTICS_E2E_SEED: "1",
        // Seed without an LLM API key so the diagnostics journey can verify
        // the backend's missing-key short-circuit deterministically, with NO
        // live OpenAI round-trip. Keeps `npm run e2e` offline-safe and avoids
        // flakiness from external network / provider latency.
        GUILDBOTICS_E2E_OFFLINE_LLM: "1",
        GUILDBOTICS_E2E_HOST: HOST,
        GUILDBOTICS_E2E_BACKEND_PORT: String(DIAGNOSTICS_BACKEND_PORT),
        GUILDBOTICS_E2E_FRONTEND_PORT: String(DIAGNOSTICS_FRONTEND_PORT),
        GUILDBOTICS_E2E_TOKEN: DIAGNOSTICS_TOKEN,
        GUILDBOTICS_E2E_CONTEXT_FILE: ".stack-context-diagnostics.json",
      },
    },
    {
      // Journey ⑥: the frontend boots pointed at DOWN_BACKEND_PORT, which is not
      // serving yet. Playwright waits on the FRONTEND url (always up), and the
      // spec uses the control server to start the backend before its retry.
      command: "node e2e/start-stack.mjs",
      url: `${DOWN_BASE_URL}/`,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        GUILDBOTICS_E2E_STACK: "down",
        GUILDBOTICS_E2E_DEFER_BACKEND: "1",
        GUILDBOTICS_E2E_HOST: HOST,
        GUILDBOTICS_E2E_BACKEND_PORT: String(DOWN_BACKEND_PORT),
        GUILDBOTICS_E2E_FRONTEND_PORT: String(DOWN_FRONTEND_PORT),
        GUILDBOTICS_E2E_CONTROL_PORT: String(DOWN_CONTROL_PORT),
        GUILDBOTICS_E2E_TOKEN: DOWN_TOKEN,
        GUILDBOTICS_E2E_CONTEXT_FILE: ".stack-context-down.json",
      },
    },
  ],
});
