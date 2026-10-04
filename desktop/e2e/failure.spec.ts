import { expect, test } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { readStackContext } from "./stack-context";

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

test("shows the backend-down error, then recovers on retry once the backend is up", async ({
  page,
}) => {
  // The initial backend-down detection rides the app's real ~45s health deadline,
  // then the retry must wait for the backend to become healthy, so budget well
  // beyond the default 60s.
  test.setTimeout(150_000);

  const ctx = readStackContext("down");
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
  // The service must also remain recoverable when selection encounters a
  // corrupt device registry; this exercises real startup and its HTTP contract.
  const machineState = join(ctx.homeDir, ".guildbotics", "data");
  mkdirSync(machineState, { recursive: true });
  const registry = join(machineState, "workspaces.json");
  writeFileSync(registry, "{");
  const response = await page.request.post(controlUrl);
  expect(response.ok()).toBe(true);
  expect((await response.json()).status).toBe("ready");

  // Retry now succeeds: Bootstrap resolves and the App mounts (sidebar nav +
  // landing service screen for the empty workspace's first-setup, whichever the
  // router lands on — the nav is the reliable "app mounted" signal).
  await retry.click();
  await expect(page.getByText("GuildBotics could not start")).toHaveCount(0, { timeout: 60_000 });
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("alert").filter({ hasText: "workspace registry" })).toHaveCount(1);
  await expect(page.getByRole("link", { name: "Setup" })).toBeVisible();

  writeFileSync(registry, "[]\n");
  const sharedGrant = join(ctx.workspaceDir, ".guildbotics", "config", "intelligences", "cli_agent_filesystem_grants.yml");
  const localGrant = join(ctx.workspaceDir, ".guildbotics", "local", "cli_agent_filesystem_grants.yml");
  mkdirSync(join(ctx.workspaceDir, ".guildbotics", "config", "intelligences"), { recursive: true });
  mkdirSync(join(ctx.workspaceDir, ".guildbotics", "local"), { recursive: true });
  writeFileSync(sharedGrant, "documents: [");
  writeFileSync(localGrant, "paths: [");
  const selected = await page.request.post(`http://${ctx.host}:${ctx.backendPort}/workspace`, {
    headers: { "X-GuildBotics-Session-Token": ctx.token },
    data: { workspace_dir: ctx.workspaceDir },
  });
  expect(selected.ok()).toBe(true);
  expect((await selected.json()).workspace_problem).toBe("");
  const statusUrl = `http://${ctx.host}:${ctx.backendPort}/intelligences/agent-environment`;
  const headers = { "X-GuildBotics-Session-Token": ctx.token };
  const broken = await page.request.get(statusUrl, { headers });
  expect((await broken.json()).access.problem).not.toBe("");
  writeFileSync(sharedGrant, "{}\n");
  writeFileSync(localGrant, "{}\n");
  const repaired = await page.request.get(statusUrl, { headers });
  expect((await repaired.json()).access.problem).toBe("");
  const recovered = await page.request.get(`http://${ctx.host}:${ctx.backendPort}/config/status`, { headers });
  expect(recovered.ok()).toBe(true);
  expect((await recovered.json()).input_store_problem).toBe("");
  await page.reload();
  await expect(page.getByRole("navigation")).toBeVisible();
  await expect(page.getByText(/Cannot read the workspace registry/)).toHaveCount(0);
});
