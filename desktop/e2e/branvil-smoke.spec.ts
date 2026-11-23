// Three real-wire journeys against the disposable service owned by start-stack.mjs.

import { expect, request, test, type Page } from "@playwright/test";
import { enter, entryEnv } from "./branvil-enter";

const env = {
  workspace: "acme",
};

test.describe.configure({ mode: "serial" });

// Script errors and CSP violations fail every test (a 404 of an optional endpoint does not).
const problems = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const list: string[] = [];
  problems.set(page, list);
  page.on("pageerror", (e) => list.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text()))
      list.push(m.text());
  });
});
test.afterEach(({ page }) => {
  expect(problems.get(page) ?? []).toEqual([]);
});

async function openTaskFlow(page: Page) {
  await enter(page, `/workspaces/${env.workspace}/task-flow`);
  await expect(page.getByRole("heading", { name: /^(Task flow|タスクフロー)$/ })).toBeVisible();
}

test("enters through the host session and keeps it across reloads", async ({ page, context }) => {
  await enter(page, "/workspaces");
  await page.reload();
  await expect(page.getByRole("link", { name: /^(Settings|設定)$/ })).toBeVisible();
  // No secret is kept in web storage.
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toMatch(/bvt_/);
  // Without the session cookie every page shows how to get one; an unknown code is refused.
  await context.clearCookies();
  await page.goto("/settings/repositories");
  await expect(page.getByText("guildbotics open", { exact: true })).toBeVisible();
  await expect(page.getByText("ssh <server> guildbotics open --print", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.goto("/enter#reused-or-made-up");
  await expect(
    page.getByText(/already been used or has expired|使用済みか、期限が切れています/),
  ).toBeVisible();
});

test("creates a ticket, moves it with its menu, and persists the move across reloads", async ({
  page,
}) => {
  await openTaskFlow(page);
  const title = `E2E ticket ${Date.now()}`;
  await page.getByRole("button", { name: /^(Add ticket|チケットを追加)$/ }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox", { name: /^(Title|タイトル)$/ }).fill(title);
  await dialog.getByRole("button", { name: /^(Create ticket|チケットを作成)$/ }).click();
  const card = page.getByRole("article", { name: new RegExp(title) });
  const newLane = card.getByRole("button", { name: /^(Lane of .*: New |.* のレーン: 新規 )/ });
  await expect(newLane).toBeVisible();

  // One representative write journey; drag and assignment branches live in component tests.
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      /^\/api\/v1\/repos\/[^/]+\/tickets\/[^/]+\/board$/.test(new URL(response.url()).pathname),
  );
  await newLane.click();
  await page.getByRole("menuitem", { name: /^(Ready|着手可)$/ }).click();
  expect((await saved).ok()).toBe(true);
  const ready = card.getByRole("button", { name: /^(Lane of .*: Ready |.* のレーン: 着手可 )/ });
  await expect(ready).toBeVisible();

  await page.reload();
  await expect(ready).toBeVisible();
});

test("shows a change made elsewhere without a reload (event stream)", async ({ page }) => {
  // The change must come after the page's event stream is open (the GUI shows no indicator for it).
  const streamOpen = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/events" && r.ok(),
  );
  await openTaskFlow(page);
  await streamOpen;
  const empty = page.getByText(/^(No tickets to show\.|表示するチケットはありません。)$/);
  await expect(page.getByRole("article").first().or(empty)).toBeVisible();
  // Another HTTP client uses the shared host session to create a ticket in the
  // workspace's first repository.
  const api = await request.newContext({
    baseURL: entryEnv.base,
    storageState: await page.context().storageState(),
  });
  const { csrf } = await (await api.get("/api/v1/session")).json();
  const repos = (await (await api.get("/api/v1/repos")).json()) as { items: { path: string }[] };
  const repo = repos.items.find((r) => r.path.startsWith(`${env.workspace}/`))?.path as string;
  const title = `Made elsewhere ${Date.now()}`;
  const created = await api.post(`/api/v1/repos/${encodeURIComponent(repo)}/tickets`, {
    headers: { "X-GuildBotics-CSRF": csrf },
    data: { title, body: "", labels: [], lane: "ready" },
  });
  expect(created.status()).toBe(201);
  await expect(page.getByRole("article", { name: new RegExp(title) })).toBeVisible();
  const ticket = (await created.json()) as { url: string; key: string };
  const posted = await api.post(`/api/v1/repos/${encodeURIComponent(repo)}/tickets/${ticket.key}/comments`, {
    headers: { "X-GuildBotics-CSRF": csrf },
    data: { body: "Opened through a comment link" },
  });
  expect(posted.status()).toBe(201);
  const comment = (await posted.json()) as { url: string; id: string };
  expect(new URL(ticket.url).pathname).toBe(`/workspaces/acme/repos/demo/tickets/${ticket.key}`);
  const tab = await page.context().newPage();
  await tab.goto(comment.url);
  const target = tab.locator(`[id="${comment.id}"]`);
  await expect(target).toHaveAttribute("data-target", "true");
  await tab.reload();
  await expect(target).toHaveAttribute("data-target", "true");
  await tab.close();
  await api.dispose();
});
