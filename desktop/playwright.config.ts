import { defineConfig, devices } from "@playwright/test";

// Real-browser E2E layer for the desktop app. Each run boots the REAL Python
// Local API backend against a fresh temp workspace; the backend serves the
// screens itself (the Vite build `npm run e2e` makes first), and each test
// enters them as the owner does, through an entry link (`e2e/stack-context.ts`).
// See `e2e/start-stack.mjs` for the harness that owns process lifecycle and the
// isolated workspace.
//
// Fully isolated stacks run in parallel, each matched to its specs by filename.
// A project is named after its stack, which is how a test's fixture finds the
// stack it enters:
//   * "setup"       — empty workspace (first-setup journey ①, `setup.spec.ts`).
//   * "configured"  — pre-seeded project + active member (service / commands
//                     journeys ③ ④, `service.spec.ts` / `commands.spec.ts`).
//   * "members"     — pre-seeded workspace dedicated to the member-add journey
//                     (journey ②, `members.spec.ts`); isolated from "configured"
//                     so mutating the member list never perturbs ③ ④.
//   * "diagnostics" — pre-seeded workspace for the readiness / scenario
//                     diagnostics journey (journey ⑤, `diagnostics.spec.ts`).
//   * "down"        — a backend the spec takes down under a loaded screen and
//                     brings back up via a control server (connection-loss
//                     journey ⑥, `failure.spec.ts`).
//   * "environment" — pre-seeded workspace given the ready isolated agent
//                     environment of the workspace GUILDBOTICS_E2E_ENVIRONMENT_FROM
//                     names, so a command runs in a real microVM (journey ⑪,
//                     `environment.spec.ts`). Started only when that is set; a
//                     device without one (CI) skips the journey.
// Each stack owns its own backend port, temp HOME (and so owner token) and
// on-disk workspace, so the journeys never share state.

const ENVIRONMENT_FROM = process.env.GUILDBOTICS_E2E_ENVIRONMENT_FROM;

/** A port the variable moves, or its default. */
function port(variable: string, fallback: number): number {
  return Number(process.env[variable] ?? fallback);
}

type Stack = {
  name: string;
  testMatch: RegExp;
  backendPort: number;
  /** What the harness is told besides the stack's name and port. */
  env?: Record<string, string>;
};

const STACKS: Stack[] = [
  {
    name: "setup",
    testMatch: /setup\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_BACKEND_PORT", 8766),
  },
  {
    name: "configured",
    testMatch: /(service|commands|decisions)\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_CONFIGURED_BACKEND_PORT", 8767),
    env: { GUILDBOTICS_E2E_SEED: "1" },
  },
  {
    name: "members",
    testMatch: /members\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_MEMBERS_BACKEND_PORT", 8768),
    env: { GUILDBOTICS_E2E_SEED: "1" },
  },
  {
    name: "diagnostics",
    testMatch: /diagnostics\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_DIAGNOSTICS_BACKEND_PORT", 8769),
    // Seed without an LLM API key so the diagnostics journey can verify the
    // backend's missing-key short-circuit deterministically, with NO live
    // OpenAI round-trip. Keeps `npm run e2e` offline-safe and avoids flakiness
    // from external network / provider latency.
    env: { GUILDBOTICS_E2E_SEED: "1", GUILDBOTICS_E2E_OFFLINE_LLM: "1" },
  },
  {
    name: "down",
    testMatch: /failure\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_DOWN_BACKEND_PORT", 8770),
    env: {
      GUILDBOTICS_E2E_CONTROL_PORT: String(port("GUILDBOTICS_E2E_DOWN_CONTROL_PORT", 8771)),
    },
  },
  {
    name: "environment",
    testMatch: /environment\.spec\.ts$/,
    backendPort: port("GUILDBOTICS_E2E_ENVIRONMENT_BACKEND_PORT", 8773),
    env: { GUILDBOTICS_E2E_SEED: "1", GUILDBOTICS_E2E_ADOPT_ENVIRONMENT: ENVIRONMENT_FROM ?? "" },
  },
];

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
  projects: STACKS.map(({ name, testMatch, backendPort }) => ({
    name,
    testMatch,
    use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${backendPort}` },
  })),
  // The harness says when its stack is ready: the backend serves its screens
  // as soon as it listens, before the workspace is seeded.
  webServer: STACKS.filter(({ name }) => name !== "environment" || ENVIRONMENT_FROM).map(
    ({ name, backendPort, env }) => ({
      command: "node e2e/start-stack.mjs",
      wait: { stdout: new RegExp(`\\[e2e:${name}\\] ready`) },
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: "pipe" as const,
      stderr: "pipe" as const,
      env: {
        ...env,
        GUILDBOTICS_E2E_STACK: name,
        GUILDBOTICS_E2E_BACKEND_PORT: String(backendPort),
      },
    }),
  ),
});
