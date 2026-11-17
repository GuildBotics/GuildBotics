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

const lane = (page: Page, name: RegExp) => page.getByRole("region", { name });

/** Opens the workspace's board. */
async function openBoard(page: Page) {
  await enter(page, `/${env.workspace}/-/board`);
  await expect(page.getByRole("heading", { name: /^(Task board|タスクボード)$/ })).toBeVisible();
}

/** Opens a card's menu and picks an item in it. */
async function fromCardMenu(page: Page, ref: RegExp, item: RegExp) {
  await page.getByRole("button", { name: ref }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

test("enters with a single-use link and keeps the session across reloads", async ({
  page,
  context,
}) => {
  await enter(page);
  await page.reload();
  await expect(page.getByRole("link", { name: /^(Settings|設定)$/ })).toBeVisible();
  // No secret is kept in web storage.
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toMatch(/bvt_/);
  // Without the session cookie every page shows how to get one; an unknown code is refused.
  await context.clearCookies();
  await page.goto("/-/settings");
  await expect(page.getByText("guildbotics branvil open --print", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/address bar of this same browser|この画面と同じブラウザのアドレスバー/),
  ).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.goto("/-/guildbotics/-/enter#reused-or-made-up");
  await expect(
    page.getByText(/already been used or has expired|使用済みか、期限が切れています/),
  ).toBeVisible();
});

test("creates a ticket, moves it with its menu, and persists the move across reloads", async ({
  page,
}) => {
  await openBoard(page);
  const title = `E2E ticket ${Date.now()}`;
  await page.getByRole("button", { name: /^(Add a ticket to New|新規 にチケットを追加)$/ }).click();
  await page.getByRole("textbox", { name: /^(Title|タイトル)$/ }).fill(title);
  await page.keyboard.press("Enter");
  const card = page.getByRole("article", { name: new RegExp(title) });
  await expect(lane(page, /^(New|新規)/).getByText(title)).toBeVisible();
  const ref = (await card.getAttribute("aria-label"))?.split(" ")[0] as string;

  // One representative write journey; drag and assignment branches live in component tests.
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "PUT" &&
      /^\/api\/v1\/repos\/[^/]+\/tickets\/[^/]+\/board$/.test(new URL(response.url()).pathname),
  );
  await fromCardMenu(page, new RegExp(`^(Actions of ${ref}|${ref} の操作)$`), /^(Ready|着手可)$/);
  expect((await saved).ok()).toBe(true);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();

  // A fresh load shows the server's state, not the optimistic one.
  await page.goto(`/${env.workspace}/-/board`);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();
});

test("shows a change made elsewhere without a reload (event stream)", async ({ page }) => {
  // The change must come after the page's event stream is open (the GUI shows no indicator for it).
  const streamOpen = page.waitForResponse(
    (r) => new URL(r.url()).pathname === "/api/v1/events" && r.ok(),
  );
  await openBoard(page);
  await streamOpen;
  // Another HTTP client uses the shared host session to create a ticket in the
  // repository the board offers first.
  const api = await request.newContext({
    baseURL: entryEnv.base,
    storageState: await page.context().storageState(),
  });
  const { csrf } = await (await api.get("/api/v1/session")).json();
  const repos = (await (await api.get("/api/v1/repos")).json()) as { items: { path: string }[] };
  const repo = repos.items.find((r) => r.path.startsWith(`${env.workspace}/`))?.path as string;
  const title = `Made elsewhere ${Date.now()}`;
  const created = await api.post(`/api/v1/repos/${encodeURIComponent(repo)}/tickets`, {
    headers: { "X-Branvil-CSRF": csrf },
    data: { title, body: "", labels: [], lane: "ready" },
  });
  expect(created.status()).toBe(201);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();
  await api.dispose();
});
