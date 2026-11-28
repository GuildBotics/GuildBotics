import { expect } from "@playwright/test";
import { ownerApi, readStackContext, test } from "./stack-context";

test("uses one sidebar and session when changing screens and workspaces", async ({
  page,
}, testInfo) => {
  await page.goto("/workspaces/acme/task-flow");
  const sidebar = page.getByRole("complementary");
  const nav = sidebar.getByRole("navigation", { name: "Main" });
  const workspace = sidebar.getByRole("combobox", { name: "Workspace" });
  const language = sidebar.getByRole("combobox", { name: "Display language" });
  await expect(workspace).toHaveValue("acme");
  await expect(nav.getByRole("link")).toHaveText([
    "Activity",
    "Task flow",
    "Timeline",
    "Pull requests",
    "Releases",
    "Service",
    "Edit Command",
    "Diagnostics",
    "Settings",
  ]);

  await language.click();
  await page.getByRole("option", { name: "日本語" }).click();
  await sidebar.getByRole("link", { name: "コマンド編集" }).click();
  await expect(page).toHaveURL(/\/workspaces\/acme\/commands$/);
  await expect(sidebar.getByRole("combobox", { name: "表示言語" })).toHaveValue("日本語");
  await expect(page.locator("html")).toHaveAttribute("lang", "ja");
  await sidebar.getByRole("combobox", { name: "表示言語" }).click();
  await page.getByRole("option", { name: "English" }).click();

  await workspace.click();
  await page.getByRole("option", { name: "+ New workspace…" }).click();
  const dialog = page.getByRole("dialog", { name: "New workspace" });
  await dialog.getByRole("textbox", { name: "Name" }).fill("navigation-test");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(page).toHaveURL(/\/workspaces\/navigation-test\/task-flow$/);
  await expect(workspace).toHaveValue("navigation-test");
  await nav.getByRole("link", { name: "Pull requests" }).click();
  await expect(page).toHaveURL(/\/workspaces\/navigation-test\/pulls$/);
  // A workspace without repositories says so on its releases page.
  await nav.getByRole("link", { name: "Releases" }).click();
  await expect(page).toHaveURL(/\/workspaces\/navigation-test\/releases$/);
  await expect(
    page.getByText("The workspace navigation-test has no repositories yet."),
  ).toBeVisible();
  await page.reload();
  await expect(workspace).toHaveValue("navigation-test");
  await expect(nav.getByRole("link", { name: "Settings" })).toHaveAttribute(
    "href",
    "/workspaces/navigation-test/setup",
  );

  const request = page.waitForRequest(
    (req) => new URL(req.url()).pathname === "/-/guildbotics/api/config/status",
  );
  await nav.getByRole("link", { name: "Edit Command" }).click();
  // The same workspace may already be cached after the deep link; force a reload.
  await page.reload();
  expect((await request).headers()["x-guildbotics-workspace"]).toBe("navigation-test");
  const ctx = readStackContext(testInfo.project.name);
  const status = await ownerApi(ctx, "/config/status");
  expect((await status.json()).workspace_name).toBe("acme");

  await page.goto("/setup?section=verification");
  await expect(page).toHaveURL(/\/workspaces\/navigation-test\/setup\?section=verification$/);
  await expect(workspace).toHaveValue("navigation-test");
  await expect(nav.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
  await workspace.click();
  await page.getByRole("option", { name: "acme", exact: true }).click();
  await expect(page).toHaveURL(/\/workspaces\/acme\/setup\?section=verification$/);
});

test("opens the repository list and editor in the same settings layout", async ({ page }) => {
  await page.goto("/workspaces/acme/setup");
  const menu = page.getByRole("navigation", { name: "Settings sections" });
  const project = menu.getByRole("link", { name: "Workspace", exact: true });
  const repos = menu.getByRole("link", { name: "Repositories" });
  await expect(project).toHaveAttribute("aria-current", "page");
  await expect(menu.getByRole("link")).toHaveText([
    "Workspace",
    "Repositories",
    "LLM / AI CLI tools",
    "Agent execution environment",
    "Integrations",
    "Members and patrol",
    "Review and authentication",
    "CI",
    "Notifications",
    "This machine",
    "Verification",
  ]);
  const originalMenu = await menu.elementHandle();
  await repos.click();
  await expect(repos).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("list", { name: "Repositories" })).toContainText("acme/demo");
  expect(await originalMenu?.evaluate((node) => node.isConnected)).toBe(true);

  await page.goto("/workspaces/acme/repos/demo/settings");
  await expect(repos).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("tab", { name: "CI", exact: true })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await project.click();
  await expect(project).toHaveAttribute("aria-current", "page");
  await expect(page).toHaveURL(/\/workspaces\/acme\/setup\?section=project$/);
});

test("opens each member screen directly and manages CI images with the agent environment", async ({
  page,
}) => {
  await page.goto("/workspaces/acme/setup");
  const menu = page.getByRole("navigation", { name: "Settings sections" });
  const section = (name: string) => menu.getByRole("link", { name, exact: true });

  await section("Members and patrol").click();
  await expect(page.getByRole("list", { name: "Members" })).toBeVisible();
  await section("Review and authentication").click();
  await expect(section("Review and authentication")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Review and authentication" })).toBeVisible();

  await section("CI").click();
  await expect(page.getByRole("region", { name: "Network" })).toContainText("acme/demo");
  await expect(page.getByRole("region", { name: "Images" })).toHaveCount(0);
  await page.getByRole("link", { name: "Manage images in Agent execution environment" }).click();
  await expect(page).toHaveURL(
    /\/workspaces\/acme\/setup\?section=environment#agent-environment-image$/,
  );
  await expect(section("Agent execution environment")).toHaveAttribute("aria-current", "page");
  const images = page.getByRole("region", { name: "Images" });
  await expect(images).toHaveCount(1);
  await expect(images.getByText("Base image")).toBeVisible();
  await expect(images.getByRole("button", { name: "Add image" })).toBeVisible();

  await section("Notifications").click();
  await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
  await expect(page.getByText("Webhook URL", { exact: true })).toBeVisible();
});

test("chooses the landing page using the viewed workspace's configuration", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("branvil.workspace", "acme"));
  await page.route("**/-/guildbotics/api/config/status", async (route) => {
    if (route.request().headers()["x-guildbotics-workspace"]) {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: { ...(await response.json()), project_file_exists: false, workspace_name: "other" },
    });
  });
  await page.goto("/");
  await expect(page).toHaveURL(/\/workspaces\/acme\/activity$/);
});
