/// <reference lib="dom" />

// Board render timing against a live, seeded Branvil (docs/performance.md, "GUI").
// Not part of the smoke run: it runs only with BRANVIL_E2E_PERF=1.
//
// Environment: BRANVIL_E2E_BASE_URL, BRANVIL_E2E_BIN and BRANVIL_E2E_DATA_DIR
// (entry links, e2e/enter.ts), BRANVIL_E2E_WORKSPACE (a workspace name; its board is timed),
// BRANVIL_E2E_PERF=1, and optionally BRANVIL_E2E_PERF_RUNS (default 3) and
// BRANVIL_E2E_CHANNEL=chrome.
//
// Run: npx playwright test e2e/perf.spec.ts --trace off
// (Playwright's trace recorder snapshots the DOM on every step, which takes
// seconds on a board of thousands of cards and would be measured too.)
//
// The times come from the page itself (milliseconds since the board page's
// navigation started), not from Playwright's polling. A timer on the page
// notes every stretch over 50 ms in which it could not run (script, style,
// layout, or anything else holding the main thread); after the first painted
// frame the run waits until there was none for 2 s, so work the browser does
// after that frame is reported too.

import { devices, test } from "@playwright/test";
import { enter } from "./enter";

const env = {
  workspace: process.env.BRANVIL_E2E_WORKSPACE,
  runs: Number(process.env.BRANVIL_E2E_PERF_RUNS ?? "3"),
};

test.skip(
  process.env.BRANVIL_E2E_PERF !== "1",
  "BRANVIL_E2E_PERF=1 runs the board timing",
);
test.setTimeout(300_000);

declare global {
  interface Window {
    __firstCardAt?: number;
    __paintedAt?: number;
    __blocked: [number, number][];
  }
}

test("time to a rendered board", async ({ browser }) => {
  for (let run = 0; run < env.runs; run++) {
    // A new browser context each run: a first visit, nothing cached.
    const context = await browser.newContext({
      ...devices["Desktop Chrome"],
      baseURL: test.info().project.use.baseURL,
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
      window.__blocked = [];
      let last = performance.now();
      const beat = () => {
        const now = performance.now();
        if (now - last > 50) window.__blocked.push([last, now - last]);
        last = now;
        setTimeout(beat, 10);
      };
      beat();
      new MutationObserver((_, observer) => {
        if (!document.querySelector("article.card")) return;
        window.__firstCardAt = performance.now();
        // The frame after the cards were inserted: they have been laid out and painted.
        requestAnimationFrame(() =>
          setTimeout(() => (window.__paintedAt = performance.now())),
        );
        observer.disconnect();
      }).observe(document, { childList: true, subtree: true });
    });
    await enter(page, `/${env.workspace}`);
    await page.waitForFunction(() => window.__paintedAt !== undefined, null, {
      polling: 500,
      timeout: 240_000,
    });
    await page.waitForFunction(
      () => {
        const last = window.__blocked.at(-1);
        return performance.now() - Math.max(window.__paintedAt ?? 0, last ? last[0] + last[1] : 0) > 2000;
      },
      null,
      { polling: 200, timeout: 240_000 },
    );
    const metrics = await page.evaluate(() => {
      const resources = performance.getEntriesByType(
        "resource",
      ) as PerformanceResourceTiming[];
      const cards = resources.filter((r) => r.name.includes("/cards"));
      const checks = resources.filter((r) => r.name.includes("/checks"));
      const memory = (
        performance as unknown as { memory?: { usedJSHeapSize: number } }
      ).memory;
      return {
        firstCardRequestAt: Math.round(
          Math.min(...cards.map((r) => r.startTime)),
        ),
        lastCardResponseAt: Math.round(
          Math.max(...cards.map((r) => r.responseEnd)),
        ),
        firstCardAt: Math.round(window.__firstCardAt ?? -1),
        paintedAt: Math.round(window.__paintedAt ?? -1),
        // The end of the last stretch over 50 ms in which the page could not
        // run a timer (nor answer input), and all such stretches together.
        busyUntil: Math.round(Math.max(0, ...window.__blocked.map(([s, d]) => s + d))),
        blockedMs: Math.round(window.__blocked.reduce((n, [, d]) => n + d, 0)),
        cards: document.querySelectorAll("article.card").length,
        cardRequests: cards.length,
        // Checks of the cards' open pull requests (the board loads up to 30).
        checkRequests: checks.length,
        lastCheckResponseAt: Math.round(
          Math.max(0, ...checks.map((r) => r.responseEnd)),
        ),
        cardBytes: cards.reduce((n, r) => n + r.transferSize, 0),
        domNodes: document.getElementsByTagName("*").length,
        heapBytes: memory?.usedJSHeapSize ?? null,
      };
    });
    console.log(JSON.stringify({ run, ...metrics }));
    await context.close();
  }
});
