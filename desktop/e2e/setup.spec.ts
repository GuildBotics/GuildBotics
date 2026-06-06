import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ①: first-run setup happy path against the REAL backend.
//
// The harness (`e2e/start-stack.mjs`) boots the Python Local API in a fresh temp
// workspace, so the app lands on first-setup. This test fills the required form
// fields exactly as `SetupPage.test.tsx` does, clicks Create, asserts the app
// transitions to the service screen, and then reads the project.yml the backend
// wrote on disk to confirm the wire actually persisted the config.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  workspaceDir: string;
  homeDir: string;
  backendPort: number;
  frontendPort: number;
  token: string;
  host: string;
};

function readStackContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("first-run setup happy path writes project.yml and enters the service view", async ({
  page,
}) => {
  const ctx = readStackContext();

  await page.goto("/");

  // The empty temp workspace has no config, so the landing redirect lands on the
  // first-setup screen.
  await expect(page.getByRole("heading", { name: "First setup" })).toBeVisible();

  // The working directory is pre-filled from the backend cwd (the temp
  // workspace). Allow for macOS /private symlink normalization.
  const workspaceField = page.getByLabel("Working directory");
  await expect
    .poll(async () => (await workspaceField.inputValue()).replace(/^\/private/, ""))
    .toBe(ctx.workspaceDir.replace(/^\/private/, ""));

  // Project section: description.
  await page.getByLabel("Project description").fill("E2E automation workspace");

  // LLM / CLI agent section: provide an API key for the default OpenAI provider.
  // The section nav buttons share text with option cards (e.g. "GitHub" vs
  // "GitHub Copilot CLI"), so match the nav buttons exactly.
  await page.getByRole("button", { name: "LLM / CLI agent", exact: true }).click();
  await page.getByLabel("OpenAI API key").fill("sk-e2e-test-key");

  // GitHub section: choose the disabled decision.
  await page.getByRole("button", { name: "GitHub", exact: true }).click();
  await page.getByRole("textbox", { name: "GitHub integration" }).click();
  await page.getByRole("option", { name: "Do not use GitHub" }).click();

  // Members section: add one active member so the section is complete. The add
  // form is shown by default and pre-filled with character defaults.
  await page.getByRole("button", { name: "Members", exact: true }).click();
  await page.getByLabel("Member ID").fill("local-agent");
  await page.getByLabel("Display name").fill("Local Agent");
  await page.getByRole("button", { name: "Add member" }).click();

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

  // The REAL backend wrote project.yml under the temp workspace.
  const projectFile = join(ctx.workspaceDir, ".guildbotics", "config", "team", "project.yml");
  const projectYaml = readFileSync(projectFile, "utf-8");
  expect(projectYaml).toContain("language: en");
  expect(projectYaml).toContain("E2E automation workspace");
});
