import assert from "node:assert/strict";
import test from "node:test";

import { verifyChromium } from "./preflight.mjs";

test("opens and closes Chromium before the journeys start", async () => {
  const calls = [];
  const launcher = {
    async launch(options) {
      calls.push(["launch", options]);
      return {
        async newPage() {
          calls.push(["newPage"]);
          return {
            async goto(url) {
              calls.push(["goto", url]);
            },
          };
        },
        async close() {
          calls.push(["close"]);
        },
      };
    },
  };

  await verifyChromium(launcher);

  assert.deepEqual(calls, [
    ["launch", { headless: true }],
    ["newPage"],
    ["goto", "data:text/plain,Chromium%20preflight"],
    ["close"],
  ]);
});

test("reports a Chromium launch failure as infrastructure", async () => {
  const launcher = {
    async launch() {
      throw new Error("bootstrap service registration denied");
    },
  };

  await assert.rejects(
    verifyChromium(launcher),
    (error) =>
      error.message.includes("E2E infrastructure failure") &&
      error.message.includes("Playwright journeys were not started") &&
      error.message.includes("bootstrap service registration denied"),
  );
});
