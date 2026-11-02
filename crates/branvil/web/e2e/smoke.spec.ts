// Browser E2E against a live Branvil (`branvil serve` serving web/dist).
//
// Environment:
//   BRANVIL_E2E_BASE_URL  base URL of the running service (e.g. http://127.0.0.1:7878)
//   BRANVIL_E2E_BIN, BRANVIL_E2E_DATA_DIR  how to get an entry link (e2e/enter.ts)
//   BRANVIL_E2E_MEMBER    the owner's member name
//   BRANVIL_E2E_WORKSPACE (optional) a workspace name, e.g. acme; tickets are made on its board,
//                         in its first repository (GET /api/v1/repos)
//   BRANVIL_E2E_PULL      (optional) an open, mergeable pull request web path, e.g. /acme/demo/-/pulls/1,
//                         written by an agent, with no required checks, closing a ticket on its workspace's board
//   BRANVIL_E2E_CHANNEL   (optional) `chrome` to use the installed Google Chrome
//
// Run: npm run build, start the service, then `npm run e2e`.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, request, test, type Page } from "@playwright/test";
import { enter, entryEnv } from "./enter";

const env = {
  base: process.env.BRANVIL_E2E_BASE_URL,
  member: process.env.BRANVIL_E2E_MEMBER,
  workspace: process.env.BRANVIL_E2E_WORKSPACE,
  pull: process.env.BRANVIL_E2E_PULL,
};

test.skip(
  !env.base || !env.member || !entryEnv.bin || !entryEnv.data,
  "BRANVIL_E2E_BASE_URL, BRANVIL_E2E_MEMBER, BRANVIL_E2E_BIN and BRANVIL_E2E_DATA_DIR are required",
);
test.describe.configure({ mode: "serial" });

// Script errors and CSP violations fail every test (a 404 of an optional endpoint does not).
const problems = new WeakMap<Page, string[]>();
test.beforeEach(({ page }) => {
  const list: string[] = [];
  problems.set(page, list);
  page.on("pageerror", (e) => list.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error" && /Content Security Policy|Refused to/i.test(m.text())) list.push(m.text());
  });
});
test.afterEach(({ page }) => {
  expect(problems.get(page) ?? []).toEqual([]);
});

const lane = (page: Page, name: RegExp) => page.getByRole("region", { name });

/** Opens the workspace's board. */
async function openBoard(page: Page) {
  await enter(page, `/${env.workspace}/-/board`);
  await expect(page.getByRole("heading", { name: /^(Board|ボード)$/ })).toBeVisible();
}

/** Opens a card's menu and picks an item in it. */
async function fromCardMenu(page: Page, ref: RegExp, item: RegExp) {
  await page.getByRole("button", { name: ref }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

test("enters with a single-use link and keeps the session across reloads", async ({ page, context }) => {
  await enter(page);
  await page.reload();
  await expect(page.getByRole("link", { name: /^(Settings|設定)$/ })).toBeVisible();
  // No secret is kept in web storage.
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(stored).not.toMatch(/bvt_/);
  // Without the session cookie every page shows how to get one; an unknown code is refused.
  await context.clearCookies();
  await page.goto("/-/settings");
  await expect(page.getByText(/branvil open/).first()).toBeVisible();
  await expect(page.getByRole("textbox")).toHaveCount(0);
  await page.goto("/-/enter#reused-or-made-up");
  await expect(page.getByText(/expired or was already used|期限切れか、すでに使われています/)).toBeVisible();
});

test("adds a ticket to a lane, drags it, moves it with its menu, and assigns and unassigns it", async ({ page }) => {
  test.skip(!env.workspace, "BRANVIL_E2E_WORKSPACE is not set");
  await openBoard(page);
  const title = `E2E ticket ${Date.now()}`;
  await page.getByRole("button", { name: /^(Add a ticket to New|新規 にチケットを追加)$/ }).click();
  await page.getByRole("textbox", { name: /^(Title|タイトル)$/ }).fill(title);
  await page.keyboard.press("Enter");
  const card = page.getByRole("article", { name: new RegExp(title) });
  await expect(lane(page, /^(New|新規)/).getByText(title)).toBeVisible();
  const ref = (await card.getAttribute("aria-label"))?.split(" ")[0] as string;

  // Drag and drop the card into the "In progress" lane.
  await card.locator("p.title").dragTo(lane(page, /^(In progress|作業中)/).getByRole("heading"));
  await expect(lane(page, /^(In progress|作業中)/).getByText(title)).toBeVisible();
  await expect(page.getByText(/moved to In progress|を 作業中 へ移動しました/)).toBeVisible();

  // The keyboard-operable alternative: the card's menu.
  await fromCardMenu(page, new RegExp(`^(Actions of ${ref}|${ref} の操作)$`), /^(Ready|着手可)$/);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();

  // The assignee is changed by pressing it.
  const assignee = new RegExp(`^(Assignee of ${ref}: |${ref} の担当者: )`);
  await fromCardMenu(page, assignee, new RegExp(`^${env.member}$`));
  await expect(card.locator(".assignee")).toHaveText(env.member as string);
  // One assignee: "No one" clears it.
  await fromCardMenu(page, assignee, /^(No one|担当なし)$/);
  await expect(card.locator(".assignee")).toHaveCount(0);

  // A fresh load shows the server's state, not the optimistic one.
  await page.goto(`/${env.workspace}/-/board`);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();
});

test("reviews and merges a pull request; the linked ticket is closed and done", async ({ page }) => {
  test.skip(!env.pull, "BRANVIL_E2E_PULL is not set");
  await enter(page, env.pull);
  await expect(page.getByRole("region", { name: /^(Description|説明)$/ })).toBeVisible();
  // The changed files open in a drawer from the facts line.
  await page.getByRole("button", { name: /file\(s\)|ファイル/ }).first().click();
  const files = page.getByRole("dialog", { name: /Changed files|変更ファイル/ });
  // A pull request of one file opens it at once; otherwise open the first.
  const first = files.locator("details.file").first();
  if ((await first.getAttribute("open")) === null) await first.locator("summary").click();
  await expect(files.locator("pre.patch")).toBeVisible();
  await files.getByRole("button", { name: /^(Close|閉じる)$/ }).click();
  await expect(page.getByRole("region", { name: /^(Checks|チェック)$/ })).toBeVisible();

  // The commit page of the reviewed head.
  const sha = (await page.locator(".compose code.sha").first().getAttribute("title")) as string;
  await page.goto(`${env.pull?.split("/-/")[0]}/-/commits/${sha}`);
  await expect(page.locator("code.sha").first()).toHaveText(sha);
  await expect(page.getByText(/Author|作成者/).first()).toBeVisible();
  await page.goBack();

  await page.getByRole("textbox", { name: /Write a comment or a review|コメントまたはレビューを書く/ }).fill("Looks good.");
  await page.getByRole("button", { name: /^(Approve|承認)$/ }).click();
  await expect(page.getByText(/Review submitted|レビューを送信しました/)).toBeVisible();
  await expect(page.getByRole("region", { name: /Conversation|会話/ }).getByText("Looks good.")).toBeVisible();

  await page.getByRole("button", { name: /Merge \(fast-forward\)|マージ（fast-forward）/ }).click();
  // The page now shows the merged pull request (the merge card is gone).
  await expect(page.getByText(/^(Merged|マージ済み)$/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Merge \(fast-forward\)|マージ（fast-forward）/ })).toHaveCount(0);

  // The ticket the pull request closes is closed and in the done lane.
  await page.getByLabel(/^(Related tickets|関連チケット)$/).getByRole("link").first().click();
  await expect(page.getByText(/^(Closed|クローズ)$/).first()).toBeVisible();
  await expect(page.getByRole("combobox", { name: /^(Lane|レーン)$/ })).toHaveValue(/^(Done|完了)$/);
});

test("shows a change made elsewhere without a reload (event stream)", async ({ page }) => {
  test.skip(!env.workspace, "BRANVIL_E2E_WORKSPACE is not set");
  // The change must come after the page's event stream is open (the GUI shows no indicator for it).
  const streamOpen = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/v1/events" && r.ok());
  await openBoard(page);
  await streamOpen;
  // Another client with a Bearer token (the owner's, read on the Hub) creates a ticket on the board,
  // in the workspace's repository the GUI's new-ticket form offers first.
  const token = readFileSync(join(entryEnv.data as string, "secrets", "owner.token"), "utf8").trim();
  const api = await request.newContext({ baseURL: env.base, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
  const repos = (await (await api.get("/api/v1/repos")).json()) as { items: { path: string }[] };
  const repo = repos.items.find((r) => r.path.startsWith(`${env.workspace}/`))?.path as string;
  const title = `Made elsewhere ${Date.now()}`;
  const created = await api.post(`/api/v1/repos/${encodeURIComponent(repo)}/tickets`, {
    data: { title, body: "", labels: [], lane: "ready" },
  });
  expect(created.status()).toBe(201);
  await expect(lane(page, /^(Ready|着手可)/).getByText(title)).toBeVisible();
  await api.dispose();
});

test("shows settings: the workspace with its repositories, and the members", async ({ page }) => {
  test.skip(!env.workspace, "BRANVIL_E2E_WORKSPACE is not set");
  await enter(page, `/${env.workspace}`);
  await page.getByRole("link", { name: /^(Settings|設定)$/ }).click();
  await expect(page.getByRole("heading", { name: /^(Settings|設定)$/, level: 2 })).toBeVisible();
  const sections = page.getByRole("navigation", { name: /Settings sections|設定の項目/ });
  await sections.getByRole("link", { name: env.workspace as string, exact: true }).click();
  await expect(page.getByRole("list", { name: /^(Repositories|リポジトリ)$/ })).toBeVisible();
  await sections.getByRole("link", { name: /^(Members|メンバー)$/ }).click();
  await expect(page.getByRole("list", { name: /^(Members|メンバー)$/ })).toBeVisible();
});

// docs/api.md: the service serves the GUI for every path that is not /api or /git.
test("app routes under /-/ survive a reload", async ({ page }) => {
  await enter(page);
  const response = await page.goto("/-/settings");
  expect(response?.status()).toBe(200);
});
