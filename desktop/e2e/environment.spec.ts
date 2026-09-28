import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { readStackContext } from "./stack-context";

// Journey ⑪: a command runs in the isolated agent environment and its result
// streams back, against the REAL backend and a REAL microVM.
//
// Every command runs in the isolated agent environment, which only a device
// that holds one can boot. The "environment" stack is such a device: the
// harness adopts the ready environment of the workspace named by
// GUILDBOTICS_E2E_ENVIRONMENT_FROM (see start-stack.mjs). Without it the stack
// is not started, and this journey is skipped.
//
// The command's step is a shell script, which runs in the environment's Linux
// whatever the host is. It reads the file its input names, as the environment
// spells a path the Local API stored for a pasted attachment, with no rewriting
// between the two, and leaves a process running that holds its output open,
// which must not hold the command.

// Inserted rather than typed, so the editor neither indents after Enter nor
// closes brackets and quotes.
const SOURCE = [
  "---",
  "name: E2E environment",
  "brain: none",
  "template_engine: jinja2",
  "inputs:",
  "  message: required",
  "commands:",
  `  - script: 'read -r path; sleep 60 & printf "%s read %s" "$(uname -s)" "$(cat "$path")"'`,
  "---",
  "{{ context.pipe }}",
].join("\n");

test.skip(
  !process.env.GUILDBOTICS_E2E_ENVIRONMENT_FROM,
  "Set GUILDBOTICS_E2E_ENVIRONMENT_FROM to a workspace whose isolated environment is ready on this device.",
);

test("runs a command in the isolated environment and streams its result", async ({ page }) => {
  // Booting a microVM takes a while on a cold device.
  test.setTimeout(180_000);
  const ctx = readStackContext("environment");

  await page.goto("/#/commands");
  await expect(page.getByRole("heading", { name: "Edit Command" })).toBeVisible();

  await page.getByRole("button", { name: "New command" }).first().click();
  await page.getByText("Create myself", { exact: true }).click();
  await page.getByRole("textbox", { name: "Command name" }).fill("e2e-environment");
  await page.getByRole("button", { name: "Create" }).click();
  await expect(page.getByText(/commands\/e2e-environment\.md$/)).toBeVisible({ timeout: 30_000 });

  await page.locator(".cm-content").click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(SOURCE);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  const commandFile = join(ctx.configDir, "commands", "e2e-environment.md");
  expect(readFileSync(commandFile, "utf-8")).toBe(SOURCE);

  // The pasted attachment is stored by the Local API in the exchange
  // directory, and its path enters the message as the environment spells it.
  const message = page.getByRole("textbox", { name: "Input text" });
  await message.evaluate((element) => {
    const clipboard = new DataTransfer();
    clipboard.items.add(new File(["e2e-image"], "clipboard.png", { type: "image/png" }));
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        bubbles: true,
        cancelable: true,
        clipboardData: clipboard,
      }),
    );
  });
  await expect(message).toHaveValue(/GuildBotics\/tmp\/session-[^/]+\/[a-f0-9]+\.png$/);

  const run = page.getByRole("button", { name: "Save and run" });
  await expect(run).toBeEnabled({ timeout: 30_000 });
  await run.click();

  // The result area reaches success through the real command.started /
  // command.finished frames, well before the process left running ends.
  await expect(page.getByText("Success")).toBeVisible({ timeout: 50_000 });
  const output = page.locator("pre.command-output").first();
  await expect(output).toContainText("Linux read e2e-image");

  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("dialog")).toBeHidden({ timeout: 30_000 });
  await expect.poll(() => existsSync(commandFile), { timeout: 30_000 }).toBe(false);
});
