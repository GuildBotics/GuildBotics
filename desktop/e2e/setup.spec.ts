import { readFileSync } from "node:fs";
import { join } from "node:path";

import { expect, test } from "@playwright/test";

import { readStackContext } from "./stack-context";

// Journey ①: first-run setup happy path against the REAL backend.
//
// The harness (`e2e/start-stack.mjs`) boots the Python Local API in a fresh temp
// workspace, so the app lands on first-setup. This test fills the required form
// fields exactly as `SetupPage.test.tsx` does, clicks Create, asserts the app
// transitions to the service screen, and then reads the project.yml the backend
// wrote on disk to confirm the wire actually persisted the config.

test("first-run setup happy path writes project.yml and enters the service view", async ({
  page,
}) => {
  const ctx = readStackContext("setup");

  await page.goto("/");

  // The empty temp workspace has no config, so the landing redirect lands on the
  // first-setup screen.
  await expect(page.getByRole("heading", { name: "First setup" })).toBeVisible();

  // The workspace is pre-filled from the stack's selected workspace root
  // (GUILDBOTICS_WORKSPACE_ROOT), never from the backend cwd.
  // Allow for macOS /private symlink normalization.
  const workspaceField = page.getByLabel("Workspace");
  await expect
    .poll(async () => (await workspaceField.inputValue()).replace(/^\/private/, ""))
    .toBe(ctx.workspaceDir.replace(/^\/private/, ""));

  // Project section: start without GitHub so the first member can be completed
  // before GitHub-specific patrol defaults are enabled.
  await page.getByLabel("Project description").fill("E2E automation workspace");
  await page.getByRole("combobox", { name: "GitHub integration" }).click();
  await page.getByRole("option", { name: "Do not use GitHub", exact: true }).click();

  // LLM / AI CLI tools section: provide an API key for the default OpenAI provider.
  // The section nav buttons share text with option cards (e.g. "GitHub" vs
  // "GitHub Copilot CLI"), so match the nav buttons exactly.
  await page.getByRole("button", { name: "LLM / AI CLI tools", exact: true }).click();
  await page.getByRole("button", { name: "Configure OpenAI API key" }).click();
  // Use an exact match so the input is not confused with the surrounding
  // "Configure OpenAI API key" action.
  await page.getByLabel("OpenAI API key", { exact: true }).fill("sk-e2e-test-key");

  // Members section (now before GitHub): add one active member so the section is
  // complete. The add form is shown by default and pre-filled with defaults.
  await page.getByRole("button", { name: "Members", exact: true }).click();
  await page.getByLabel("Member ID").fill("local-agent");
  await page.getByLabel("Display name").fill("Local Agent");
  await page.getByRole("combobox", { name: "Roles" }).click();
  await page.getByRole("option", { name: "product" }).click();
  await page.getByRole("button", { name: "Add member" }).click();

  // Enable GitHub after the member draft is complete, then provide the Project
  // URL. Both live in the Project section; the GitHub section derives its lane
  // fields from the URL typed here, and shows only a "not set" notice until it
  // parses.
  await page.getByRole("button", { name: "Project", exact: true }).click();
  await page.getByRole("combobox", { name: "GitHub integration" }).click();
  await page.getByRole("option", { name: "Use GitHub", exact: true }).click();
  await page
    .getByRole("textbox", { name: "GitHub Project URL" })
    .fill("https://github.com/orgs/acme/projects/9");

  // Override the lane mapping with custom status names. The backend cannot
  // reach GitHub in CI, so the status-options fetch reports unavailable and the
  // lane fields behave as free text; the typed names must still persist.
  await page.getByRole("button", { name: "GitHub", exact: true }).click();
  await page.getByRole("textbox", { name: "Ready lane" }).fill("Ready");
  await page.getByRole("textbox", { name: "Working lane" }).fill("Doing");
  await page.getByRole("textbox", { name: "Done lane" }).fill("Shipped");

  // Create the initial settings.
  const createButton = page.getByRole("button", { name: "Create initial settings" });
  await expect(createButton).toBeEnabled();
  await createButton.click();

  // The app reports success and switches into settings (configured) mode.
  await expect(page.getByText("Initial settings created")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  // Navigate to the service view and confirm the transition.
  await page.getByRole("link", { name: "Service" }).click();
  await expect(page).toHaveURL(/#\/service$/);
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();

  // The REAL backend wrote project.yml under the temp workspace, including the
  // custom lane_map typed into the GitHub section.
  const projectFile = join(ctx.workspaceDir, ".guildbotics", "config", "team", "project.yml");
  const projectYaml = readFileSync(projectFile, "utf-8");
  expect(projectYaml).toContain("language: en");
  expect(projectYaml).toContain("E2E automation workspace");
  expect(projectYaml).toContain("lane_map:");
  expect(projectYaml).toContain("ready: Ready");
  expect(projectYaml).toContain("working: Doing");
  expect(projectYaml).toContain("done: Shipped");

  // Effort overlay: the settings UI builds typed controls from the descriptor
  // the backend serves for the provider. jsdom can only prove the editor state
  // changed; that a number survives client -> FastAPI -> YAML as a number needs
  // this real stack. Branch coverage stays in the unit / component tests.
  await page.getByRole("link", { name: "Setup" }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByRole("button", { name: "LLM / AI CLI tools", exact: true }).click();
  await page.getByRole("button", { name: "Advanced settings", exact: true }).click();

  // Switch the slot to a provider whose effort setting is an integer, so the
  // typed control has a number to carry all the way to YAML.
  await page.getByRole("combobox", { name: "Provider" }).click();
  await page.getByRole("option", { name: "Google Gemini", exact: true }).click();
  await page.getByRole("button", { name: "Customize" }).first().click();
  await page.getByLabel("high thinking_budget").fill("4096");
  await page.getByLabel("low thinking_budget").fill("0");

  // Saving is deliberate: one button writes the basic settings and the advanced
  // editor, and the advanced half is composed against what the basic half just
  // wrote. Nothing reaches disk until it is pressed.
  await page.getByRole("button", { name: "Save", exact: true }).click();

  // Poll until the backend has written the file. The read tolerates a
  // not-yet-written file so the poll can keep waiting.
  const modelFile = join(
    ctx.workspaceDir,
    ".guildbotics",
    "config",
    "intelligences",
    "models",
    "gemini",
    "default.yml",
  );
  const readModelFile = () => {
    try {
      return readFileSync(modelFile, "utf-8");
    } catch {
      return "";
    }
  };
  await expect.poll(readModelFile, { timeout: 15_000 }).toContain("thinking_budget: 4096");
  const modelYaml = readModelFile();
  expect(modelYaml).toContain("thinking_budget: 0");
  // An integer must not arrive quoted; a provider would reject that.
  expect(modelYaml).not.toContain("'4096'");
});
