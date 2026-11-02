// Layout regression coverage with fixture API responses; no service credentials
// or data are used. Serve the GUI, then set BRANVIL_E2E_BASE_URL and optionally
// BRANVIL_E2E_CHANNEL as for the other browser tests.
import { expect, test } from "@playwright/test";

test.skip(!process.env.BRANVIL_E2E_BASE_URL, "BRANVIL_E2E_BASE_URL must serve the GUI.");

const AUTHOR = "build-bot-with-a-name-that-needs-to-be-truncated";
const AT = "2026-10-23T12:00:00Z";
const SHA = "a".repeat(40);
const STATES = [
  { checks: [{ name: "test", status: "completed", conclusion: "success" }], checks_expected: true },
  { checks: [{ name: "test", status: "completed", conclusion: "failure" }], checks_expected: true },
  { checks: [{ name: "test", status: "in_progress", conclusion: null }], checks_expected: true },
  { checks: [], checks_expected: true },
  { checks: [], checks_expected: false },
];
const COUNTS = [0, 1, 4, 12, 4];
const pulls = STATES.map((_, i) => ({
  number: 100000 + i, label: `!${100000 + i}`, repository: "acme/demo",
  url: `http://localhost/acme/demo/-/pulls/${100000 + i}`,
  title: `Layout fixture ${i}: a long title remains readable through its pull request`,
  author: AUTHOR, state: "open", merged: false, draft: true,
  head: `feature/${i}`, head_sha: SHA, base: "main", base_sha: SHA,
  reviewers: [], review_states: Array.from({ length: COUNTS[i] }, (_, n) => ({
    member: `review-bot-${n}`, requested: n % 4 === 3,
    verdict: ["approve", "request_changes", "comment", null][n % 4], outdated: n % 3 === 2,
  })), approvals: 0, tickets: [], labels: [], changed_files: 0,
  created_at: AT, updated_at: AT, revision: 1, upstream: null,
}));
const repo = { path: "acme/demo", default_branch: "main", required_checks: [], protected_branches: [], required_approvals: 0, autoland: "off", upstream: null };
const pageOf = (items: unknown[]) => ({ items, next_cursor: null });

for (const locale of ["en-US", "ja-JP"]) {
  test.describe(locale, () => {
    test.use({ locale });
    for (const width of [800, 1280, 1600]) {
      test(`keeps every status and review mark readable at ${width}px`, async ({ page }, info) => {
        await page.setViewportSize({ width, height: 1000 });
        await page.route("**/api/v1/**", async (route) => {
          const path = decodeURIComponent(new URL(route.request().url()).pathname);
          let body: unknown;
          if (path.endsWith("/session")) body = { me: { name: "owner", kind: "owner" }, csrf: "fixture-only" };
          else if (path.endsWith("/workspaces")) body = pageOf([{ name: "acme", lanes: [] }]);
          else if (path.endsWith("/members")) body = pageOf([]);
          else if (path.endsWith("/diagnostics")) body = {};
          else if (path.endsWith("/repos")) body = pageOf([repo]);
          else if (path.endsWith("/repos/acme/demo")) body = repo;
          else if (path.endsWith("/pulls")) body = pageOf(pulls);
          else if (path.endsWith("/checks")) {
            const number = Number(path.match(/\/pulls\/(\d+)\//)?.[1]);
            body = { head_sha: SHA, base_sha: SHA, behind_by: 0, base_checks: 0, ...STATES[number - 100000] };
          } else if (path.endsWith("/events")) {
            await route.fulfill({ status: 200, contentType: "text/event-stream", body: ": fixture\n\n" });
            return;
          } else {
            await route.fulfill({ status: 404, json: { error: { code: "not_found", message: "No fixture branch" } } });
            return;
          }
          await route.fulfill({ json: body });
        });
        await page.goto("/acme/-/pulls");
        const table = page.getByRole("table");
        await expect(table.getByRole("row")).toHaveCount(6);
        await expect(table.locator('[role="cell"]:nth-child(5) .mantine-Badge-root')).toHaveCount(5);
        const issues = await table.evaluate((element) => {
          const issues: string[] = [];
          const header = [...element.querySelectorAll('[role="columnheader"]')].map((cell) => cell.getBoundingClientRect().x);
          for (const row of element.querySelectorAll("a[role=row]")) {
            const cells = [...row.querySelectorAll<HTMLElement>(':scope > [role="cell"]')];
            cells.forEach((cell, i) => {
              if (Math.abs(cell.getBoundingClientRect().x - header[i]) > 1) issues.push(`column ${i} is misaligned`);
            });
            for (const cell of [cells[3], cells[4]]) {
              for (const label of cell.querySelectorAll<HTMLElement>(".mantine-Badge-label")) {
                if (label.scrollWidth > label.clientWidth + 1) issues.push(`clipped badge: ${label.textContent}`);
              }
            }
            const reviewer = cells[6].getBoundingClientRect();
            for (const part of cells[6].querySelectorAll(".review-mark-anchor, .review-mark")) {
              const box = part.getBoundingClientRect();
              if (box.left < reviewer.left - 1 || box.right > reviewer.right + 1 || box.top < reviewer.top - 1 || box.bottom > reviewer.bottom + 1) issues.push("clipped reviewer");
            }
            const author = cells[5].getBoundingClientRect();
            for (const part of cells[5].children) {
              if (part.getBoundingClientRect().right > author.right + 1) issues.push("overflowing author");
            }
          }
          if (document.documentElement.scrollWidth > innerWidth + 1) issues.push("page overflows horizontally");
          return issues;
        });
        expect(issues).toEqual([]);
        for (const [i, count] of COUNTS.entries()) {
          await expect(table.locator("a[role=row]").nth(i).locator(".review-mark-anchor")).toHaveCount(count);
        }
        // A narrow window scrolls the list without losing its rightmost column.
        if (width === 800) {
          expect(await table.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
          await table.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
          expect(await table.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
        }
        await page.screenshot({ path: info.outputPath("pull-list.png"), fullPage: true });
      });
    }
  });
}
