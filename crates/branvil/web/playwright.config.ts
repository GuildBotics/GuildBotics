import { defineConfig, devices } from "@playwright/test";

// Real journeys own a disposable service; fixture layout and performance runs are opt-in.
const visual = process.env.BRANVIL_E2E_VISUAL === "1";
const perf = process.env.BRANVIL_E2E_PERF === "1";
const baseURL = process.env.BRANVIL_E2E_BASE_URL ?? "http://127.0.0.1:7889";
process.env.BRANVIL_E2E_BASE_URL = baseURL;

export default defineConfig({
  testDir: "./e2e",
  testMatch: visual ? /.*-layout\.spec\.ts$/ : perf ? /perf\.spec\.ts$/ : /smoke\.spec\.ts$/,
  globalSetup: visual || perf ? undefined : "./e2e/start-stack.ts",
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  // BRANVIL_E2E_CHANNEL=chrome uses the installed Google Chrome instead of Playwright's Chromium download.
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], channel: process.env.BRANVIL_E2E_CHANNEL } }],
});
