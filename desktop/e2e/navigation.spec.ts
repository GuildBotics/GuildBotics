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
    "Pull requests",
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
  const project = menu.getByRole("button", { name: "Project" });
  const repos = menu.getByRole("link", { name: "Repositories" });
  await expect(project).toHaveAttribute("aria-current", "page");
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
