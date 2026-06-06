import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ④: Commands against the REAL backend.
//
// The "configured" harness pre-seeds the temp workspace, which also installs the
// sample commands (translate / summarize / get-time-of-day / context-info). This
// journey selects the `context-info` sample command — a deterministic, no-LLM,
// no-GitHub command (`brain: none`, jinja2 template) — runs it through the REAL
// `/commands/run` endpoint, and asserts that the run output and the
// command.started / command.finished `/events` websocket frames surface in the
// run history.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  memberId: string | null;
  configDir: string;
};

function readConfiguredContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-configured.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("runs the seeded context-info command and shows real output + events", async ({ page }) => {
  const ctx = readConfiguredContext();

  await page.goto("/#/commands");
  await expect(page.getByRole("heading", { name: "Run Command" })).toBeVisible();

  // The seeded active member is preselected; no blocked alert should be shown.
  await expect(page.getByText("No active members")).toHaveCount(0);

  // Select the deterministic sample command from the searchable catalog. The
  // option's accessible name also carries the description (custom renderOption),
  // so match the label substring rather than an exact string.
  const commandSelect = page.getByRole("textbox", { name: "Command", exact: true });
  await commandSelect.click();
  await commandSelect.fill("context-info");
  await page.getByRole("option", { name: /Context Info \(context-info\)/ }).click();
  // The selected command surfaces its on-disk script path; assert on that rather
  // than the search input's internal value (Mantine keeps the typed query there).
  await expect(page.getByRole("link", { name: /context-info\.md$/ })).toBeVisible({
    timeout: 30_000,
  });

  // Run it against the REAL backend.
  const run = page.getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeEnabled();
  await run.click();

  // The run history surfaces the request and reaches a terminal success state,
  // driven by the real command.started / command.finished websocket frames and
  // the /commands/run response.
  await expect(page.getByText(/^Request /)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Success")).toBeVisible({ timeout: 30_000 });

  // The Output tab shows the deterministic template output rendered by the
  // backend for the seeded member + team (jinja2, no LLM).
  const output = page.locator("pre.command-output").first();
  await expect(output).toContainText("Language code: en", { timeout: 30_000 });
  await expect(output).toContainText(`ID: ${ctx.memberId}`);
  await expect(output).toContainText("Name: Local Agent");

  // The Events tab lists the real command.* websocket frames for this request.
  await page.getByRole("tab", { name: "Events" }).click();
  await expect(page.getByText("started")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("finished")).toBeVisible({ timeout: 30_000 });
});
