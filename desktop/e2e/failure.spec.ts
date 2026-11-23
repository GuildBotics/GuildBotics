import { expect } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { ownerApi, readStackContext, test, type StackContext } from "./stack-context";

// Journey ⑥: critical failure — the server goes away under a loaded screen,
// then comes back.
//
// The "down" stack's spec takes the REAL backend down and brings it back up
// through a control server (see start-stack.mjs `GUILDBOTICS_E2E_CONTROL_PORT`).
// Each control answer comes only once the backend is gone, or answers again, so
// neither the screen noticing the loss nor the retry can race the backend. The
// session survives the restart: the server keeps it beside the owner token,
// which still authenticates.

async function control(ctx: StackContext, action: "stop-backend" | "start-backend") {
  const response = await fetch(`http://${ctx.host}:${ctx.controlPort}/control/${action}`, {
    method: "POST",
  });
  expect(response.ok).toBe(true);
}

test("keeps the screen under a notice while the server is unreachable, then recovers on retry", async ({
  page,
}) => {
  test.setTimeout(150_000);

  const ctx = readStackContext("down");
  await page.goto("/");
  await expect(page.getByRole("navigation")).toBeVisible({ timeout: 60_000 });

  await control(ctx, "stop-backend");
  // The next request the screen makes finds nobody: opening another screen
  // makes one at once.
  await page.evaluate(() => (window.location.hash = "#/diagnostics"));
  const unreachable = page.getByRole("dialog", { name: "Cannot reach GuildBotics" });
  await expect(unreachable).toBeVisible({ timeout: 30_000 });
  // What was on the screen stays under the notice.
  await expect(page.getByRole("navigation")).toBeAttached();

  // The service must also remain recoverable when selection encounters a
  // corrupt device registry; this exercises real startup and its HTTP contract.
  const machineState = join(ctx.homeDir, ".guildbotics", "data");
  mkdirSync(machineState, { recursive: true });
  const registry = join(machineState, "workspaces.json");
  writeFileSync(registry, "{");
  await control(ctx, "start-backend");

  // Retry reaches the server again and reloads what the screen shows.
  await unreachable.getByRole("button", { name: "Retry" }).click();
  await expect(unreachable).toHaveCount(0, { timeout: 60_000 });
  await expect(page.getByRole("navigation")).toBeVisible();
  await expect(page.getByRole("alert").filter({ hasText: "workspace registry" })).toHaveCount(1);
  await expect(page.getByRole("link", { name: "Setup" })).toBeVisible();

  writeFileSync(registry, "[]\n");
  const sharedGrant = join(
    ctx.workspaceDir,
    ".guildbotics",
    "config",
    "intelligences",
    "cli_agent_filesystem_grants.yml",
  );
  const localGrant = join(
    ctx.workspaceDir,
    ".guildbotics",
    "local",
    "cli_agent_filesystem_grants.yml",
  );
  mkdirSync(join(ctx.workspaceDir, ".guildbotics", "config", "intelligences"), { recursive: true });
  mkdirSync(join(ctx.workspaceDir, ".guildbotics", "local"), { recursive: true });
  writeFileSync(sharedGrant, "documents: [");
  writeFileSync(localGrant, "paths: [");
  const selected = await ownerApi(ctx, "/workspace", {
    method: "POST",
    body: JSON.stringify({ workspace_dir: ctx.workspaceDir }),
  });
  expect(selected.ok).toBe(true);
  expect((await selected.json()).workspace_problem).toBe("");
  const broken = await ownerApi(ctx, "/intelligences/agent-environment");
  expect((await broken.json()).access.problem).not.toBe("");
  writeFileSync(sharedGrant, "{}\n");
  writeFileSync(localGrant, "{}\n");
  const repaired = await ownerApi(ctx, "/intelligences/agent-environment");
  expect((await repaired.json()).access.problem).toBe("");
  const recovered = await ownerApi(ctx, "/config/status");
  expect(recovered.ok).toBe(true);
  expect((await recovered.json()).input_store_problem).toBe("");
  await page.reload();
  await expect(page.getByRole("navigation")).toBeVisible();
  await expect(page.getByText(/Cannot read the workspace registry/)).toHaveCount(0);
});
