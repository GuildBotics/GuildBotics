import assert from "node:assert/strict";
import test from "node:test";

import {
  environmentValue,
  npmInvocation,
  withEnvironment,
  withoutEnvironment,
} from "./environment.mjs";
import { verifyChromium } from "./preflight.mjs";

test("normalizes Windows environment keys while isolating the stack", () => {
  const environment = withEnvironment(
    { Path: "C:\\tools", Home: "C:\\real-home", USERPROFILE: "C:\\real-profile" },
    {
      PATH: `C:\\stubs;${environmentValue({ Path: "C:\\tools" }, "PATH")}`,
      HOME: "C:\\temp-home",
      USERPROFILE: "C:\\temp-home",
    },
  );

  assert.deepEqual(environment, {
    PATH: "C:\\stubs;C:\\tools",
    HOME: "C:\\temp-home",
    USERPROFILE: "C:\\temp-home",
  });
  assert.deepEqual(withoutEnvironment(environment, ["path", "userprofile"]), {
    HOME: "C:\\temp-home",
  });
});

test("launches npm through its JavaScript entrypoint", () => {
  assert.deepEqual(npmInvocation({ npm_execpath: "C:\\npm-cli.js" }, "node.exe"), {
    executable: "node.exe",
    arguments: ["C:\\npm-cli.js"],
  });
  assert.throws(() => npmInvocation({}, "node"), /launched through an npm script/);
});

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

test("preserves the infrastructure failure when Chromium cleanup also fails", async () => {
  const launcher = {
    async launch() {
      return {
        async newPage() {
          throw new Error("page creation denied");
        },
        async close() {
          throw new Error("browser cleanup failed");
        },
      };
    },
  };

  await assert.rejects(
    verifyChromium(launcher),
    (error) =>
      error.message.includes("E2E infrastructure failure") &&
      error.message.includes("page creation denied") &&
      !error.message.includes("browser cleanup failed"),
  );
});
