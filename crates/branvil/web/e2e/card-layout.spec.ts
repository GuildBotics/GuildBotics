// Layout regression coverage of ticket cards with fixture API responses; no
// service credentials or data are used. Serve the GUI, then set
// BRANVIL_E2E_BASE_URL and optionally BRANVIL_E2E_CHANNEL as for the other
// browser tests.
import { expect, test } from "@playwright/test";

test.skip(!process.env.BRANVIL_E2E_BASE_URL, "BRANVIL_E2E_BASE_URL must serve the GUI.");

const AT = "2026-10-28T12:00:00Z";
const REPO = "GuildBotics/Branvil";
const ref = (key: string) => ({ url: `http://localhost/${REPO}/-/tickets/${key}`, repository: REPO, key, title: `Ticket ${key}`, state: "open", state_reason: null });
const card = (key: string, lane: string, extra: Record<string, unknown>) => ({
  url: `http://localhost/${REPO}/-/tickets/${key}`, repository: REPO, key, title: `Ticket ${key}`, body: "",
  state: "open", state_reason: null, author: "owner", assignee: null, labels: [], interactive: false,
  interactive_ancestor: null, upstream_issue: null, lane, rank: `0|${key}`, parent: null, children: [],
  blocked_by: [], blocks: [], created_at: AT, updated_at: AT, closed_at: null, revision: 1, comments: [],
  pull_requests: [], ...extra,
});
const CARDS = [
  // First rows of every length up to the menu in the top right corner: one of them ends under it.
  ...["1", "12", "123", "1234", "12345", "123456", "1234567"].map((key) => card(key, "in_progress", { interactive: true })),
  card("120", "in_progress", { interactive_ancestor: ref("1"), blocked_by: [ref("9")] }),
  // A word longer than the card.
  card("115", "ready", { title: "Investigate: test_termination_after_browser_failure_stops_the_child sometimes times out" }),
];
const pageOf = (items: unknown[]) => ({ items, next_cursor: null });

for (const locale of ["en-US", "ja-JP"]) {
  test.describe(locale, () => {
    test.use({ locale });
    for (const path of ["/GuildBotics/-/board", "/GuildBotics"]) {
      test(`keeps every card's contents inside it and clear of its menu on ${path}`, async ({ page }, info) => {
        await page.setViewportSize({ width: 1280, height: 1000 });
        await page.route("**/api/v1/**", async (route) => {
          const url = decodeURIComponent(new URL(route.request().url()).pathname);
          let body: unknown;
          if (url.endsWith("/session")) body = { me: { name: "owner", kind: "owner" }, csrf: "fixture-only" };
          else if (url.endsWith("/workspaces")) body = pageOf([{ name: "GuildBotics", lanes: ["new", "ready", "in_progress", "done"] }]);
          else if (url.endsWith("/workspaces/GuildBotics")) body = { name: "GuildBotics", lanes: ["new", "ready", "in_progress", "done"] };
          else if (url.endsWith("/board/cards")) body = pageOf(CARDS);
          else if (url.endsWith("/members") || url.endsWith("/repos")) body = pageOf([]);
          else if (url.endsWith("/diagnostics")) body = {};
          else if (url.endsWith("/events")) {
            await route.fulfill({ status: 200, contentType: "text/event-stream", body: ": fixture\n\n" });
            return;
          } else {
            await route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No fixture branch" } } });
            return;
          }
          await route.fulfill({ json: body });
        });
        await page.goto(path);
        await expect(page.locator("article.card")).toHaveCount(CARDS.length);
        const issues = await page.locator("article.card").evaluateAll((cards) => {
          const issues: string[] = [];
          const overlaps = (a: DOMRect, b: DOMRect) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
          for (const card of cards) {
            const name = card.getAttribute("aria-label");
            const box = card.getBoundingClientRect();
            const menu = card.querySelector(".card-menu")?.getBoundingClientRect();
            for (const part of card.querySelectorAll<HTMLElement>(".card-head > *")) {
              if (menu && overlaps(part.getBoundingClientRect(), menu)) issues.push(`${name}: "${part.textContent}" under the menu`);
            }
            for (const part of card.querySelectorAll<HTMLElement>(".title a, .card-row > *")) {
              const r = part.getBoundingClientRect();
              if (r.left < box.left - 1 || r.right > box.right + 1) issues.push(`${name}: "${part.textContent}" outside the card`);
            }
          }
          return issues;
        });
        expect(issues).toEqual([]);
        await page.screenshot({ path: info.outputPath("cards.png"), fullPage: true });
      });
    }
  });
}
