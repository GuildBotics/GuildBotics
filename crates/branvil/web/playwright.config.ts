import { defineConfig, devices } from "@playwright/test";

// Runs against a live `branvil serve` that serves this GUI. Not part of
// `npm run test`; the environment variables are listed in e2e/smoke.spec.ts.
const baseURL = process.env.BRANVIL_E2E_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
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
