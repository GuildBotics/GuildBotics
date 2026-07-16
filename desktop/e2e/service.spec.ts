import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Locator, type Page } from "@playwright/test";

// Journey ③: Service Runtime against the REAL backend.
//
// The "configured" harness (`e2e/start-stack.mjs` with GUILDBOTICS_E2E_SEED=1)
// pre-seeds the temp workspace with a project + one active member (GitHub
// integration disabled), so the app boots already-configured.
//
// This journey starts only the event-trigger source (chat handling), observes
// the UI reach a running state driven by the REAL backend status/websocket
// events, then stops and observes stopped. It proves that the three source
// toggles select the runtime units sent to the backend.

const here = dirname(fileURLToPath(import.meta.url));

const RUN_BUTTON = "Run";
const STOP_BUTTON = "Stop";

type StackContext = {
  workspaceDir: string;
  memberId: string | null;
  seeded: boolean;
};

function readConfiguredContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-configured.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

function panel(page: Page, title: string): Locator {
  return page.locator(".service-unit-panel").filter({ hasText: title }).first();
}

// Mantine renders the switch as a visually-hidden <input> with a visible track
// rendered from the input's <label>, so the input itself is not click-actionable
// in a real browser. Clicking the visible track toggles it the way a user would.
async function toggleTarget(panelLocator: Locator): Promise<void> {
  await panelLocator.locator(".service-unit-switch .mantine-Switch-track").click();
}

async function ensureStopped(page: Page): Promise<void> {
  const stop = page.getByRole("button", { name: STOP_BUTTON });
  if (await stop.isVisible().catch(() => false)) {
    await stop.click();
    await expect(page.getByRole("button", { name: RUN_BUTTON })).toBeVisible({ timeout: 30_000 });
  }
}

test.beforeEach(async ({ page }) => {
  // Each test starts from a stopped runtime, regardless of order. The service
  // screen only renders the Stop button while running, so this no-ops when the
  // runtime is already stopped.
  await page.goto("/#/service");
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();
  await ensureStopped(page);
});

test("seeded workspace boots configured and lands on the service screen", () => {
  const ctx = readConfiguredContext();
  expect(ctx.seeded).toBe(true);
  expect(ctx.memberId).toBe("local-agent");
});

test("starts the events target, reaches running via real events, then stops", async ({ page }) => {
  await page.goto("/#/service");
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();

  const patrolPanel = panel(page, "Patrol commands");
  const eventsPanel = panel(page, "Event triggers");
  const scheduledPanel = panel(page, "Scheduled commands");

  // Disable the GitHub-requiring patrol target and scheduled commands so the
  // event triggers alone are started.
  await toggleTarget(patrolPanel);
  await toggleTarget(scheduledPanel);

  const start = page.getByRole("button", { name: RUN_BUTTON });
  await expect(start).toBeEnabled();
  await start.click();

  // The REAL backend reports the events unit running; the UI swaps Start for Stop
  // and the events panel state badge reads "Running". The state badge lives in the
  // panel title row (the "Stopped" field label below would otherwise collide).
  const stop = page.getByRole("button", { name: STOP_BUTTON });
  await expect(stop).toBeVisible({ timeout: 30_000 });
  await expect(eventsPanel.locator(".service-unit-title").getByText("Running")).toBeVisible({
    timeout: 30_000,
  });
  // The disabled patrol target never reaches a running state.
  await expect(patrolPanel.locator(".service-unit-title").getByText("Running")).toHaveCount(0);

  // Stop and observe the runtime return to a stopped state driven by the backend.
  await stop.click();
  await expect(start).toBeVisible({ timeout: 30_000 });
  await expect(eventsPanel.locator(".service-unit-title").getByText("Stopped")).toBeVisible({
    timeout: 30_000,
  });
});
