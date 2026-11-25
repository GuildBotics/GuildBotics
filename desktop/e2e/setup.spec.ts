import { readFileSync, realpathSync } from "node:fs";
import { join } from "node:path";

import { expect } from "@playwright/test";

import { ownerApi, readStackContext, test } from "./stack-context";

// Follow Initial Setup in README.md and README.ja.md starting at the browser
// entry from `guildbotics open`. The fixture starts the real Rust backend with
// an empty workspace and uses test credentials without contacting providers.

test("follows the README's initial setup, saves the workspace, and opens Service", async ({
  page,
}) => {
  const ctx = readStackContext("setup");
  const projectFile = join(ctx.workspaceDir, ".guildbotics", "config", "team", "project.yml");
  const readProject = () => JSON.parse(readFileSync(projectFile, "utf-8"));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "First setup" })).toBeVisible();

  await test.step("1. Confirm the workspace and describe the project", async () => {
    await expect(
      page.getByRole("link", { name: "Workspace", exact: true, current: "page" }),
    ).toBeVisible();
    const workspace = page.getByRole("textbox", { name: "Workspace", exact: true });
    // Compare filesystem identities across platform-specific spellings.
    await expect
      .poll(async () => realpathSync.native(await workspace.inputValue()))
      .toBe(realpathSync.native(ctx.workspaceDir));
    await page.getByLabel("Project description").fill("E2E automation workspace");
  });

  await test.step("2. Configure the LLM and AI CLI tool", async () => {
    await page.getByRole("link", { name: "LLM / AI CLI tools", exact: true }).click();
    await page.getByRole("button", { name: "Codex", exact: true }).click();
    await page.getByRole("button", { name: "Configure OpenAI API key" }).click();
    await page.getByLabel("OpenAI API key", { exact: true }).fill("sk-e2e-test-key");
  });

  await test.step("3. Leave chat unused", async () => {
    await page.getByRole("link", { name: "Integrations", exact: true }).click();
    const chat = page.getByRole("combobox", { name: "Chat", exact: true });
    await expect(chat).toHaveValue("Not used");
    await chat.click();
    await expect(page.getByRole("option")).toHaveText(["Not used", "Slack"]);
    await chat.press("Escape");
  });

  await test.step("4. Add an active member", async () => {
    await page.getByRole("link", { name: "Members and patrol", exact: true }).click();
    await page.getByLabel("Member ID").fill("local-agent");
    await page.getByLabel("Display name").fill("Local Agent");
    await page.getByRole("combobox", { name: "Roles", exact: true }).click();
    await page.getByRole("option", { name: "product" }).click();
    await page.getByRole("button", { name: "Add member" }).click();
    const members = page.getByRole("list", { name: "Members" });
    await expect(members.getByText("Local Agent (local-agent)", { exact: true })).toBeVisible();
  });

  await test.step("5. Create the initial settings and open Service", async () => {
    await page.getByRole("button", { name: "Create initial settings" }).click();
    await expect(page.getByText("Initial settings created")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await page.getByRole("link", { name: "Service", exact: true }).click();
    await expect(page).toHaveURL(/\/service$/);
    await expect(page.getByRole("heading", { name: "Service Runtime" })).toBeVisible();
    expect(readProject()).toMatchObject({
      language: "en",
      description: "E2E automation workspace",
    });
    expect(readProject().services).toEqual({
      code_hosting_service: { name: "branvil" },
      ticket_service: { name: "branvil" },
    });
    const settings = await ownerApi(ctx, "/config/project");
    expect(settings.ok).toBe(true);
    expect(await settings.json()).toMatchObject({ llm_api_type: "openai", cli_agent: "codex" });
  });

  await test.step("Enable Slack after initial setup", async () => {
    await page.getByRole("link", { name: "Settings", exact: true }).click();
    await page.getByRole("link", { name: "Integrations", exact: true }).click();
    await page.getByRole("combobox", { name: "Chat", exact: true }).click();
    await page.getByRole("option", { name: "Slack", exact: true }).click();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect
      .poll(() => readProject().services)
      .toEqual({
        chat_service: { name: "slack" },
        code_hosting_service: { name: "branvil" },
        ticket_service: { name: "branvil" },
      });
  });

  const memberFile = join(ctx.configDir, "team", "members", "local-agent", "member.yml");
  await page.getByRole("link", { name: "Members and patrol", exact: true }).click();
  const editMember = page
    .getByRole("list", { name: "Members" })
    .getByRole("button", { name: "Edit", exact: true });
  const slackTab = page.getByRole("tab", { name: "Slack", exact: true });
  await editMember.click();
  await page.getByRole("tab", { name: "Basic", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Display name", exact: true })).toHaveValue(
    "Local Agent",
  );
  await slackTab.click();
  await page.getByLabel("Slack Bot token", { exact: true }).fill("xoxb-e2e-test");
  await page.getByLabel("Slack App token", { exact: true }).fill("xapp-e2e-test");
  await page.getByRole("textbox", { name: "Channel", exact: true }).fill("C0123456789");
  await page.getByRole("button", { name: "Add channel", exact: true }).click();
  await page.getByRole("textbox", { name: "Channel", exact: true }).first().fill("random");
  await page.getByRole("button", { name: "Add channel", exact: true }).click();
  const saveMember = async () => {
    const saved = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/-/guildbotics/api/config/members/local-agent" &&
        response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    expect((await saved).status()).toBe(200);
  };
  await saveMember();
  const member = JSON.parse(readFileSync(memberFile, "utf-8"));
  expect(member.message_channels.map((channel: { chat: object }) => channel.chat)).toEqual([
    { enabled: true, participation: "strict", channel_id: "C0123456789" },
    { enabled: true, participation: "strict", channel_name: "random" },
  ]);
  await page.reload();
  await editMember.click();
  await slackTab.click();
  await expect(page.locator('input[value="C0123456789"]')).toBeVisible();
  await expect(page.locator('input[value="random"]')).toBeVisible();

  // Effort overlay: the settings UI builds typed controls from the descriptor
  // the backend serves for the provider. jsdom can only prove the editor state
  // changed; that a number survives client -> FastAPI -> YAML as a number needs
  // this real stack. Branch coverage stays in the unit / component tests.
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  await page.getByRole("link", { name: "LLM / AI CLI tools", exact: true }).click();
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
  await expect.poll(readModelFile, { timeout: 15_000 }).toMatch(/"?thinking_budget"?:\s*4096/);
  const modelYaml = readModelFile();
  expect(modelYaml).toMatch(/"?thinking_budget"?:\s*0/);
  // An integer must not arrive quoted; a provider would reject that.
  expect(modelYaml).not.toContain("'4096'");
});

test("one entry opens both screens, APIs and the consent page", async ({ page, context }) => {
  await expect.poll(async () => (await page.request.get("/api/v1/session")).status()).toBe(200);
  const gb = await (await page.request.get("/-/guildbotics/api/session")).json();
  const bv = await (await page.request.get("/api/v1/session")).json();
  expect(bv.me.kind).toBe("owner");
  expect(bv.csrf).toBe(gb.csrf);
  const write = (csrf: string) =>
    page.request.post("/api/v1/workspaces", {
      headers: { "x-guildbotics-csrf": csrf },
      data: { name: "shared-session" },
    });
  expect((await write("wrong")).status()).toBe(403);
  expect((await write(bv.csrf)).status()).toBe(201);
  const link = await (
    await page.request.post("/-/guildbotics/api/session/links", {
      headers: { "x-guildbotics-csrf": bv.csrf },
      data: { next: "/" },
    })
  ).json();
  expect(new URL(link.url).pathname).toBe("/enter");
  await context.clearCookies();
  await page.goto(link.url);
  await expect(page).toHaveURL(/\/(?:activity|setup)$/);
  await page.goto("/workspaces/shared-session/task-flow");
  await expect(page.getByRole("heading", { name: /^(Task flow|タスクフロー)$/ })).toBeVisible();
  const query = new URLSearchParams({
    client: "test-app",
    redirect_uri: "http://127.0.0.1:12345/callback",
    state: "fixture",
    code_challenge: "a".repeat(43),
    code_challenge_method: "S256",
  });
  await page.goto(`/authorize?${query}`);
  await expect(page.getByRole("button", { name: /^(Allow|許可する|許可)$/ })).toBeVisible();
  await page.goto("/");
  expect((await page.request.get("/-/guildbotics/api/session")).status()).toBe(200);
  expect(
    (await page.request.post("/api/v1/session", { data: { code: "old-code" } })).status(),
  ).toBe(405);
});

test("unsigned consent resumes through the common entry", async ({ page, context }) => {
  const query = new URLSearchParams({
    client: "test-app",
    redirect_uri: "http://127.0.0.1:12345/callback",
    state: "fixture",
    code_challenge: "a".repeat(43),
    code_challenge_method: "S256",
  });
  const path = "/authorize";
  await context.clearCookies();
  await page.goto(`${path}?${query}`);
  await expect(page.getByRole("button", { name: /^(Allow|許可する|許可)$/ })).toHaveCount(0);
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("guildbotics.entryReturn")))
    .not.toBeNull();
  const response = await ownerApi(readStackContext("setup"), "/session/links", {
    method: "POST",
    body: JSON.stringify({ next: "/" }),
  });
  const { url } = await response.json();
  await page.goto(url);
  await expect(page).toHaveURL(new RegExp(path.replaceAll("/", "\\/") + "\\?"));
  await expect(page.getByRole("button", { name: /^(Allow|許可する|許可)$/ })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("guildbotics.entryReturn"))).toBeNull();
});
