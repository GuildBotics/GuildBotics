import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ④: Commands against the REAL backend.
//
// The "configured" harness pre-seeds the temp workspace, which also installs the
// sample commands (translate / summarize / get-time-of-day / context-info). This
// journey first verifies the declared summarize arguments, then selects the
// deterministic `context-info` sample command — a no-LLM, no-GitHub command
// (`brain: none`, jinja2 template) — runs it through the REAL `/commands/run`
// endpoint, and asserts that the run output and normalized trace records surface
// in the run history.

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
  await commandSelect.fill("summarize");
  await page.getByRole("option", { name: /Summarize \(summarize\)/ }).click();
  await expect(page.getByRole("link", { name: /summarize\.md$/ })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole("textbox", { name: "file" })).toHaveAttribute("required", "");
  const language = page.getByRole("textbox", { name: "language", exact: true });
  await expect(language).not.toHaveAttribute("required");
  await expect(language).toHaveAttribute("placeholder", "English");
  await expect(page.getByRole("button", { name: "Run", exact: true })).toBeDisabled();

  await commandSelect.click();
  await commandSelect.fill("context-info");
  await page.getByRole("option", { name: /Context Info \(context-info\)/ }).click();
  // The selected command surfaces its on-disk script path; assert on that rather
  // than the search input's internal value (Mantine keeps the typed query there).
  await expect(page.getByRole("link", { name: /context-info\.md$/ })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole("textbox", { name: "Additional args" })).toHaveCount(0);
  await expect(page.getByRole("textbox", { name: "Input text" })).toHaveCount(0);

  // Run it against the REAL backend.
  const run = page.getByRole("button", { name: "Run", exact: true });
  await expect(run).toBeEnabled();
  await run.click();

  // The run history surfaces the request and reaches a terminal success state,
  // driven by the real command.started / command.finished websocket frames and
  // the /commands/run response.
  await expect(page.locator(".command-panel").getByText(/^Trace /)).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText("Success")).toBeVisible({ timeout: 30_000 });

  // The Output tab shows the deterministic template output rendered by the
  // backend for the seeded member + team (jinja2, no LLM).
  const output = page.locator("pre.command-output").first();
  await expect(output).toContainText("Language code: en", { timeout: 30_000 });
  await expect(output).toContainText(`ID: ${ctx.memberId}`);
  await expect(output).toContainText("Name: Local Agent");

  // The Events tab reuses the diagnostics trace timeline backed by the real
  // trace-detail endpoint.
  await page.getByRole("tab", { name: "Events" }).click();
  const timeline = page.locator(".exec-timeline");
  await expect(timeline.getByText("Started", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(timeline.getByText("Finished", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(
    page.locator(".exec-timeline-toolbar").getByText("AI", { exact: true }),
  ).toBeVisible();
});
