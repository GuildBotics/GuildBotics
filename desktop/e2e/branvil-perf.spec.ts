/// <reference lib="dom" />

// Small-fixture board timing only: the disposable acme workspace has one card.
// It cannot reproduce the historical 1,695-card benchmark in crates/branvil/docs/performance.md.
// The harness accepts neither a seeded data directory nor a workspace override;
// use these timings only to investigate this fixture, never as large-board results.
// Not part of the smoke run: it runs only with GUILDBOTICS_E2E_PERF=1.
//
// Run from desktop with GUILDBOTICS_E2E_PERF=1 npm run e2e -- --project=branvil.
// The stack uses a disposable workspace and the host's shared browser session.
// Disable tracing when measuring: its DOM snapshots distort board timings.
//
// The times come from the page itself (milliseconds since the board page's
// navigation started), not from Playwright's polling. A timer on the page
// notes every stretch over 50 ms in which it could not run (script, style,
// layout, or anything else holding the main thread); after the first painted
// frame the run waits until there was none for 2 s, so work the browser does
// after that frame is reported too.

import { devices, test } from "@playwright/test";
import { enter, entryEnv } from "./branvil-enter";

const env = {
  workspace: "acme",
  runs: Number(process.env.GUILDBOTICS_E2E_PERF_RUNS ?? "3"),
};

test.skip(process.env.GUILDBOTICS_E2E_PERF !== "1", "GUILDBOTICS_E2E_PERF=1 runs the board timing");
test.setTimeout(300_000);

declare global {
  interface Window {
    __firstCardAt?: number;
    __paintedAt?: number;
    __blocked: [number, number][];
  }
}

test("time to a rendered board", async ({ browser }) => {
  const seed = await browser.newPage({ baseURL: entryEnv.base });
  await enter(seed);
  const { csrf } = await (await seed.request.get("/api/v1/session")).json();
  const response = await seed.request.post("/api/v1/repos/acme%2Fdemo/tickets", {
    headers: { "X-GuildBotics-CSRF": csrf },
    data: { title: "Board timing fixture", lane: "ready" },
  });
  if (!response.ok()) throw new Error(`timing fixture: ${response.status()}`);
  await seed.close();
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
        requestAnimationFrame(() => setTimeout(() => (window.__paintedAt = performance.now())));
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
        return (
          performance.now() - Math.max(window.__paintedAt ?? 0, last ? last[0] + last[1] : 0) > 2000
        );
      },
      null,
      { polling: 200, timeout: 240_000 },
    );
    const metrics = await page.evaluate(() => {
      const resources = performance.getEntriesByType("resource") as PerformanceResourceTiming[];
      const cards = resources.filter((r) => r.name.includes("/cards"));
      const checks = resources.filter((r) => r.name.includes("/checks"));
      const memory = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory;
      return {
        firstCardRequestAt: Math.round(Math.min(...cards.map((r) => r.startTime))),
        lastCardResponseAt: Math.round(Math.max(...cards.map((r) => r.responseEnd))),
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
        lastCheckResponseAt: Math.round(Math.max(0, ...checks.map((r) => r.responseEnd))),
        cardBytes: cards.reduce((n, r) => n + r.transferSize, 0),
        domNodes: document.getElementsByTagName("*").length,
        heapBytes: memory?.usedJSHeapSize ?? null,
      };
    });
    console.log(
      JSON.stringify({
        workload: "disposable-acme-one-card",
        comparableToHistoricalBenchmark: false,
        run,
        ...metrics,
      }),
    );
    await context.close();
  }
});
