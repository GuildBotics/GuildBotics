import { readFileSync, realpathSync } from "node:fs";
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
  // Compare filesystem identities rather than spellings: macOS may add
  // /private, while Windows may expand an 8.3 temp path before returning it.
  const workspaceField = page.getByLabel("Workspace");
  await expect
    .poll(async () => realpathSync.native(await workspaceField.inputValue()))
    .toBe(realpathSync.native(ctx.workspaceDir));

  // Project section.
  await page.getByLabel("Project description").fill("E2E automation workspace");

  // LLM / AI CLI tools section: provide an API key for the default OpenAI provider.
  // The section nav buttons share text with option cards (e.g. "GitHub" vs
  // "GitHub Copilot CLI"), so match the nav buttons exactly.
  await page.getByRole("button", { name: "LLM / AI CLI tools", exact: true }).click();
  await page.getByRole("button", { name: "Configure OpenAI API key" }).click();
  // Use an exact match so the input is not confused with the surrounding
  // "Configure OpenAI API key" action.
  await page.getByLabel("OpenAI API key", { exact: true }).fill("sk-e2e-test-key");

  // Integrations section: one provider per kind of service, chosen from what
  // the backend offers. The board is named by its URL; the backend cannot reach
  // GitHub here, so the status-options read reports unavailable and the lane
  // fields behave as free text, whose names must still persist.
  await page.getByRole("button", { name: "Integrations", exact: true }).click();
  const choose = async (kind: string, provider: string) => {
    await page.getByRole("combobox", { name: kind, exact: true }).click();
    await page.getByRole("option", { name: provider, exact: true }).click();
  };
  await choose("Tickets", "GitHub");
  await page
    .getByRole("textbox", { name: "GitHub Project URL" })
    .fill("https://github.com/orgs/acme/projects/9");
  await page.getByRole("textbox", { name: "Ready lane" }).fill("Ready");
  await page.getByRole("textbox", { name: "Working lane" }).fill("Doing");
  await page.getByRole("textbox", { name: "Done lane" }).fill("Shipped");
  await choose("Code hosting", "GitHub");
  // The code host's owner starts as the board's.
  await expect(page.getByRole("textbox", { name: "Owner", exact: true })).toHaveValue("acme");
  await choose("Chat", "Slack");

  // Members section: add one active member so the section is complete; its
  // account tabs are those of the providers chosen above. The add form is shown by default and pre-filled with defaults.
  await page.getByRole("button", { name: "Members", exact: true }).click();
  await page.getByLabel("Member ID").fill("local-agent");
  await page.getByLabel("Display name").fill("Local Agent");
  await page.getByRole("combobox", { name: "Roles", exact: true }).click();
  await page.getByRole("option", { name: "product" }).click();
  await page.getByRole("button", { name: "Add member" }).click();
  const memberList = page.getByRole("list", { name: "Members" });
  await expect(memberList.getByText("Local Agent (local-agent)", { exact: true })).toBeVisible();

  // Create the initial settings.
  const createButton = page.getByRole("button", { name: "Create initial settings" });
  await expect(createButton).toBeEnabled();
  await createButton.click();

  // The app reports success and switches into settings (configured) mode.
  await expect(page.getByText("Initial settings created")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  // Navigate to the service view and confirm the transition. Nav links are
  // matched in full: the system-alert band on a device without an agent
  // environment carries "Open setup" links that a substring match would also
  // resolve to.
  await page.getByRole("link", { name: "Service", exact: true }).click();
  await expect(page).toHaveURL(/#\/service$/);
  await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();

  // The REAL backend wrote project.yml under the temp workspace: each kind's
  // provider and only that provider's items, the custom lane_map included.
  const projectFile = join(ctx.workspaceDir, ".guildbotics", "config", "team", "project.yml");
  const projectYaml = readFileSync(projectFile, "utf-8");
  expect(projectYaml).toContain("language: en");
  expect(projectYaml).toContain("E2E automation workspace");
  expect(projectYaml).toContain("  code_hosting_service:\n    name: github\n    owner: acme\n");
  expect(projectYaml).toContain(
    [
      "  ticket_service:",
      "    name: github",
      "    url: https://github.com/orgs/acme/projects/9",
      "    owner: acme",
      "    project_id: '9'",
      "    lane_map:",
      "      ready: Ready",
      "      working: Doing",
      "      done: Shipped",
    ].join("\n"),
  );
  expect(projectYaml).toContain("  chat_service:\n    name: slack\n");

  const memberFile = join(ctx.configDir, "team", "members", "local-agent", "member.yml");
  await page.goto("/#/setup?section=members&person_id=local-agent&tab=slack");
  await page.getByRole("tab", { name: "Basic", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Display name", exact: true })).toHaveValue(
    "Local Agent",
  );
  await page.getByRole("tab", { name: "Slack", exact: true }).click();
  await page.getByLabel("Slack Bot token", { exact: true }).fill("xoxb-e2e-test");
  await page.getByLabel("Slack App token", { exact: true }).fill("xapp-e2e-test");
  await page.getByRole("textbox", { name: "Channel", exact: true }).fill("C0123456789");
  await page.getByRole("button", { name: "Add channel", exact: true }).click();
  await page.getByRole("textbox", { name: "Channel", exact: true }).first().fill("random");
  await page.getByRole("button", { name: "Add channel", exact: true }).click();
  const notification = page.getByRole("switch", { name: /Notify when work stops/ }).first();
  await expect(notification).toBeVisible({ timeout: 5000 });
  await notification.check();
  const saveMember = async () => {
    const saved = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/config/members/local-agent" &&
        response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    expect((await saved).status()).toBe(200);
  };
  await saveMember();
  expect(readFileSync(memberFile, "utf-8")).toContain("notify_work_stops: true");
  await page.reload();
  await expect(notification).toBeChecked();
  await page.getByRole("combobox", { name: "Display language", exact: true }).click();
  await page.getByRole("option", { name: "Japanese", exact: true }).click();
  await expect(page.getByRole("switch", { name: /作業停止を通知する/ }).first()).toBeChecked();
  await page.getByRole("combobox", { name: "表示言語", exact: true }).click();
  await page.getByRole("option", { name: "English", exact: true }).click();
  await notification.uncheck();
  await saveMember();
  expect(readFileSync(memberFile, "utf-8")).toContain("notify_work_stops: false");

  // Effort overlay: the settings UI builds typed controls from the descriptor
  // the backend serves for the provider. jsdom can only prove the editor state
  // changed; that a number survives client -> FastAPI -> YAML as a number needs
  // this real stack. Branch coverage stays in the unit / component tests.
  await page.getByRole("link", { name: "Setup", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByRole("button", { name: "LLM / AI CLI tools", exact: true }).click();
  await page.getByRole("button", { name: "Advanced settings", exact: true }).click();

  // Switch the slot to a provider whose effort setting is an integer, so the
  // typed control has a number to carry all the way to YAML. The name is the
  // slot's own label in full: a substring would also accept the next control
  // whose label happens to contain it.
  await page.getByRole("combobox", { name: "LLM provider", exact: true }).click();
  const providerOption = page.getByRole("option", { name: "Google Gemini", exact: true });
  // The page is still settling while the provider and CLI-tool cards land, and
  // Mantine hides an open dropdown for good the moment floating-ui calls the
  // trigger clipped -- nothing brings it back, so the control keeps saying
  // `aria-expanded="true"` over a list that is not rendered (#427). A dropdown
  // that claims to be open has to still be showing its list after the page
  // moves under it, which is a real-browser measurement jsdom cannot make.
  await page.mouse.wheel(0, 400);
  await page.mouse.wheel(0, -400);
  // floating-ui remeasures on the frames after the scroll, so give the latch
  // its chance to happen before asserting that it did not.
  await page.waitForTimeout(200);
  await expect(providerOption).toBeVisible();
  await providerOption.click();
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
