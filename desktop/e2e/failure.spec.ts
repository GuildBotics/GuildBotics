import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ⑥: critical failure — backend DOWN at app load, then recover.
//
// The "down" stack boots the frontend pointed at a backend port that is NOT yet
// serving (see start-stack.mjs `GUILDBOTICS_E2E_DEFER_BACKEND`). On load the app's
// Bootstrap calls startBackend() → waitForHealth() against the dead port, which
// fails its real deadline and renders the error alert + a Retry button.
//
// Recovery is made DETERMINISTIC (not timing-flaky) by a control server: the spec
// POSTs `/control/start-backend`, which starts the REAL backend and only answers
// 200 AFTER `/health` is green. The subsequent Retry click therefore always finds
// a healthy backend, so the app loads. The 45s real health deadline in the app's
// waitForHealth is why this journey needs an extended timeout.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  controlPort: number;
  host: string;
  deferBackend: boolean;
};

function readDownContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-down.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("shows the backend-down error, then recovers on retry once the backend is up", async ({
  page,
}) => {
  // The initial backend-down detection rides the app's real ~45s health deadline,
  // then the retry must wait for the backend to become healthy, so budget well
  // beyond the default 60s.
  test.setTimeout(150_000);

  const ctx = readDownContext();
  expect(ctx.deferBackend).toBe(true);

  await page.goto("/");

  // Backend is down: Bootstrap surfaces the failure alert and a Retry button.
  // This appears only after the app's real waitForHealth deadline elapses.
  await expect(page.getByText("GuildBotics could not start")).toBeVisible({ timeout: 60_000 });
  const retry = page.getByRole("button", { name: "Retry" });
  await expect(retry).toBeVisible();
  // The app is NOT mounted while the backend is unreachable.
  await expect(page.getByRole("navigation")).toHaveCount(0);

  // Bring the REAL backend up on demand via the control server. The control
  // endpoint only returns once `/health` is green, so the retry below cannot race
  // ahead of a ready backend.
  const controlUrl = `http://${ctx.host}:${ctx.controlPort}/control/start-backend`;
  const response = await page.request.post(controlUrl);
  expect(response.ok()).toBe(true);
  expect((await response.json()).status).toBe("ready");

  // Retry now succeeds: Bootstrap resolves and the App mounts (sidebar nav +
  // landing service screen for the empty workspace's first-setup, whichever the
  // router lands on — the nav is the reliable "app mounted" signal).
  await retry.click();
  await expect(page.getByText("GuildBotics could not start")).toHaveCount(0, { timeout: 60_000 });
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 60_000 });
});
