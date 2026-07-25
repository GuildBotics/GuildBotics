import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ④: Command editor against the REAL backend.
//
// The "configured" harness pre-seeds a temp workspace (project + one active
// member). This journey opens the command editor, creates a new Markdown
// command, edits its source, saves-and-runs it through the REAL
// `/commands/files` + `/commands/run` endpoints, then asserts the edited source
// reached disk and the run output + trace records surface in the result area,
// and finally deletes the command so the file leaves the workspace again.
//
// A `brain: none` Markdown command is deterministic (no LLM / GitHub), so the
// rendered body is echoed back as the run output.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  memberId: string | null;
  configDir: string;
};

function readConfiguredContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-configured.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

const SOURCE = [
  "---",
  "name: E2E note",
  "brain: none",
  "inputs:",
  "  message: hidden",
  "---",
  "E2E marker body",
].join("\n");

test("creates, edits, saves and runs a shared command", async ({ page }) => {
  const ctx = readConfiguredContext();

  await page.goto("/#/commands");
  await expect(page.getByRole("heading", { name: "Edit Command" })).toBeVisible();

  // Create a new Markdown command through the dialog.
  await page.getByRole("button", { name: "New command" }).first().click();
  await page.getByRole("textbox", { name: "Command name" }).fill("e2e-note");
  await page.getByRole("button", { name: "Create" }).click();

  // The editor loads the new file (its path row shows the shared location).
  await expect(page.getByText(/commands\/e2e-note\.md$/)).toBeVisible({ timeout: 30_000 });

  // Replace the source with a deterministic brain:none command.
  const editor = page.locator(".cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.type(SOURCE);

  await expect(page.getByText("Unsaved changes")).toBeVisible();

  // Save-and-run against the REAL backend.
  const run = page.getByRole("button", { name: "Save and run" });
  await expect(run).toBeEnabled({ timeout: 30_000 });
  await run.click();

  // The result area reaches a terminal success state driven by the real
  // command.started / command.finished websocket frames.
  await expect(page.getByText("Success")).toBeVisible({ timeout: 30_000 });

  // The output tab echoes the rendered body (no LLM).
  const output = page.locator("pre.command-output").first();
  await expect(output).toContainText("E2E marker body", { timeout: 30_000 });

  // The edited source actually reached disk.
  const commandFile = join(ctx.configDir, "commands", "e2e-note.md");
  const onDisk = readFileSync(commandFile, "utf-8");
  expect(onDisk).toContain("E2E marker body");
  expect(onDisk).toContain("brain: none");

  // Deleting through the confirm dialog removes the real file from the
  // workspace, not just the entry in the list.
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("dialog")).toBeHidden({ timeout: 30_000 });
  await expect.poll(() => existsSync(commandFile), { timeout: 30_000 }).toBe(false);
});
