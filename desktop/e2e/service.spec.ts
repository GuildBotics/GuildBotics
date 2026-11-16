import { expect, type Locator, type Page } from "@playwright/test";

import { readStackContext, test } from "./stack-context";

// Journey ③: Service Runtime against the REAL backend.
//
// The "configured" harness (`e2e/start-stack.mjs` with GUILDBOTICS_E2E_SEED=1)
// pre-seeds the temp workspace with a project + one active member, every kind
// of service on the reference provider, so the app boots already-configured.
//
// This journey starts only the event-trigger source (chat handling), observes
// the UI reach a running state driven by the REAL backend status/websocket
// events, then stops and observes stopped. It proves that the three source
// toggles, saved on the backend, select the runtime units it starts.

const RUN_BUTTON = "Run";
const STOP_BUTTON = "Stop";

function panel(page: Page, title: string): Locator {
  return page.locator(".service-unit-panel").filter({ hasText: title }).first();
}

// Mantine renders the switch as a visually-hidden <input> with a visible track
// rendered from the input's <label>, so the input itself is not click-actionable
// in a real browser. Clicking the visible track toggles it the way a user would.
// The choice is saved on the backend and outlives the test, so the switch is
// set to what the test needs rather than flipped.
async function setTarget(panelLocator: Locator, included: boolean): Promise<void> {
  const target = panelLocator.getByRole("switch", { name: "Include in service run" });
  // The switches show the saved settings once they have loaded.
  await expect(panelLocator.page().getByRole("textbox", { name: /interval/i })).not.toHaveValue("");
  if ((await target.isChecked()) !== included) {
    await expect(target).toBeEnabled();
    await panelLocator.locator(".service-unit-switch .mantine-Switch-track").click();
  }
  await expect(target).toBeChecked({ checked: included });
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
  await page.goto("/-/guildbotics/#/service");
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();
  await ensureStopped(page);
});

test("seeded workspace boots configured and lands on the service screen", () => {
  const ctx = readStackContext("configured");
  expect(ctx.seeded).toBe(true);
  expect(ctx.memberId).toBe("local-agent");
});

test("keeps warning bands at their content height on a long page and preserves the fill page", async ({
  page,
}) => {
  // Supply exceptional notices without stopping or corrupting the real stack.
  // The service page and its content still come from the seeded backend.
  const workspaceProblem = "The selected workspace cannot be opened. Choose another workspace.";
  const inputProblem =
    "The input store cannot be read. Repair the configuration before continuing.";
  await page.route("**/api/config/status", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        workspace_problem: workspaceProblem,
        input_store_problem: inputProblem,
      },
    });
  });
  await page.route("**/api/hub-status", (route) =>
    route.fulfill({
      json: {
        current: null,
        target: null,
        operation: {
          kind: "update",
          phase: "draining",
          since: "2026-11-17T00:00:00Z",
          waiting: { runs: 3, ci_jobs: 2 },
        },
      },
    }),
  );
  await page.setViewportSize({ width: 1000, height: 650 });
  await page.reload();
  const workspace = page.locator(".workspace");
  const bands = workspace.locator(":scope > .mantine-Alert-root");
  await expect(bands).toHaveCount(3);
  await expect(bands.filter({ hasText: workspaceProblem })).toHaveCount(1);
  await expect(bands.filter({ hasText: inputProblem })).toHaveCount(1);
  await expect(bands.filter({ hasText: "Hub maintenance" })).toHaveCount(1);
  expect(await workspace.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  for (const band of await bands.all()) {
    await expect
      .poll(() => band.evaluate((el) => el.scrollHeight - el.clientHeight))
      .toBeLessThanOrEqual(1);
  }

  await page.setViewportSize({ width: 1000, height: 1100 });
  await page.goto("/-/guildbotics/#/commands");
  const fill = workspace.locator(":scope > .workspace-fill");
  await expect(fill).toBeVisible();
  await expect(page.locator(".cm-content")).toBeVisible();
  // With notices above it, the editor must take exactly the remaining space.
  const remainingSpace = () =>
    fill.evaluate((el) => {
      const parent = el.parentElement!;
      const style = getComputedStyle(parent);
      return (
        parent.getBoundingClientRect().bottom -
        Number.parseFloat(style.paddingBottom) -
        el.getBoundingClientRect().bottom
      );
    });
  await expect.poll(remainingSpace).toBeCloseTo(0, 0);
  const tall = await fill.evaluate((el) => el.getBoundingClientRect().height);
  await page.setViewportSize({ width: 1000, height: 1050 });
  await expect.poll(remainingSpace).toBeCloseTo(0, 0);
  await expect
    .poll(() => fill.evaluate((el) => el.getBoundingClientRect().height))
    .toBeCloseTo(tall - 50, 0);
});

test("starts the events target, reaches running via real events, then stops", async ({ page }) => {
  await page.goto("/-/guildbotics/#/service");
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();

  const patrolPanel = panel(page, "Patrol commands");
  const eventsPanel = panel(page, "Event triggers");
  const scheduledPanel = panel(page, "Scheduled commands");

  // Leave the event triggers alone in the run: the last source left on cannot
  // be switched off, so it is switched on first.
  await setTarget(eventsPanel, true);
  await setTarget(patrolPanel, false);
  await setTarget(scheduledPanel, false);

  const start = page.getByRole("button", { name: RUN_BUTTON });
  await expect(start).toBeEnabled();
  const starting = page.waitForResponse(
    (r) => r.url().endsWith("/scheduler/start") && r.request().method() === "POST",
  );
  await start.click();
  const response = await starting;
  expect(response.status(), await response.text()).toBe(200);

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
