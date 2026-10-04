import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { readStackContext } from "./stack-context";

// Journey ④: Command editor against the REAL backend.
//
// The "configured" harness pre-seeds a temp workspace (project + one active
// member). This journey opens the command editor, creates a new Markdown
// command, edits and saves its source through the REAL `/commands/files`
// endpoints, asserts the edited source reached disk, and finally deletes the
// command so the file leaves the workspace again.
//
// Every command runs in the isolated agent environment, which this stack's
// device cannot hold (see start-stack.mjs): the command's requirements, read
// from the REAL backend, refuse the run before it starts, and say why.

// Every line starts at column 0 on purpose: the editor keeps the previous
// line's indentation when Enter is typed, so an indented block (such as a
// nested `inputs:` mapping) would push the closing `---` and the body out of
// column 0 and leave the frontmatter unterminated. The `message` input keeps
// its default `optional` policy, which the run does not need to fill in.
const SOURCE = ["---", "name: E2E note", "brain: none", "---", "E2E marker body"].join("\n");

test("creates, edits and saves a shared command, and says why it cannot run here", async ({
  page,
}) => {
  const ctx = readStackContext("configured");

  await page.goto("/#/commands");
  await expect(page.getByRole("heading", { name: "Edit Command" })).toBeVisible();

  // The assistant is a drawer, as on the diagnostics screen: the editor owns
  // the full width until it is asked for, and closing it hands the width back.
  await expect(page.getByRole("region", { name: "AI assistant" })).toBeHidden();
  await page.getByRole("button", { name: "Ask AI" }).click();
  await expect(page.getByRole("region", { name: "AI assistant" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("region", { name: "AI assistant" })).toBeHidden();

  // Create a new Markdown command through the dialog.
  await page.getByRole("button", { name: "New command" }).first().click();
  await expect(page.getByRole("textbox", { name: "What should this command do?" })).toBeVisible();
  await page.getByText("Create myself", { exact: true }).click();
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
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();

  // Browser-preview mode cannot expose native dropped paths, but it exercises
  // the other half of the attachment contract: a pasted clipboard image is
  // persisted by the REAL Local API in GuildBotics' own storage (under the
  // stack's own HOME), which no microVM writes, and its path, as the
  // environment spells it, enters the message.
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
  await expect(message).toHaveValue(
    /[/\\]\.guildbotics[/\\]data[/\\]command_inputs[/\\]session-[^/\\]+[/\\][a-f0-9]+\.png$/,
  );
  const pastedImage = await message.inputValue();
  const storedPath = pastedImage.match(/[/\\](session-[^/\\]+)[/\\]([a-f0-9]+\.png)$/);
  expect(storedPath).not.toBeNull();
  const [, session, name] = storedPath as RegExpMatchArray;
  const storedImage = join(ctx.homeDir, ".guildbotics", "data", "command_inputs", session, name);
  expect(readFileSync(storedImage, "utf-8")).toBe("e2e-image");

  // The run is refused on this device, in the words the environment's
  // status gives.
  await expect(page.getByText(/^Isolated environment: .+/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Save and run" })).toBeDisabled();

  // The edited source actually reached disk, byte for byte: an editor that
  // reformats what was typed (auto-indent, bracket closing) would break the
  // frontmatter and must fail here with a readable diff.
  const commandFile = join(ctx.configDir, "commands", "e2e-note.md");
  expect(readFileSync(commandFile, "utf-8")).toBe(SOURCE);

  // Deleting through the confirm dialog removes the real file from the
  // workspace, not just the entry in the list.
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("dialog")).toBeHidden({ timeout: 30_000 });
  await expect.poll(() => existsSync(commandFile), { timeout: 30_000 }).toBe(false);
});
