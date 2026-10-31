import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { test as base, type Page } from "@playwright/test";

// Spec-side reader for the run context published by `e2e/start-stack.mjs`, and
// the way a spec reaches its stack: as the owner, through the owner token the
// stack's server wrote.
//
// The context describes ONE run's live processes (temp workspace, temp HOME,
// ports, seeded ids), so it lives in the OS temp dir next to the temp dirs it
// points at, never inside the repository. The path is derived from the stack
// name alone, which both sides already know statically, so no handshake is
// needed. `start-stack.mjs` mirrors this derivation on the writer side.

export function stackContextPath(stackName: string): string {
  return join(tmpdir(), "guildbotics-e2e", `${stackName}.json`);
}

export type StackContext = {
  stackName: string;
  workspaceDir: string;
  homeDir: string;
  configDir: string;
  /** Log the harness's AI CLI tool stubs append to when they are launched. */
  cliStubLog: string;
  backendPort: number;
  controlPort: number;
  host: string;
  seeded: boolean;
  seededWithoutLlmKey: boolean;
  memberId: string | null;
};

export function readStackContext(stackName: string): StackContext {
  return JSON.parse(readFileSync(stackContextPath(stackName), "utf-8")) as StackContext;
}

/**
 * A request to the stack's API as its owner: with the Bearer token the server
 * wrote to its owner token file (`guildbotics/app_api/auth.py`), read where
 * `start-stack.mjs` reads it.
 */
export function ownerApi(ctx: StackContext, path: string, init: RequestInit = {}) {
  const token = readFileSync(
    join(ctx.homeDir, ".guildbotics", "data", "server", String(ctx.backendPort), "owner.token"),
    "utf-8",
  ).trim();
  return fetch(`http://${ctx.host}:${ctx.backendPort}/api${path}`, {
    ...init,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
}

/**
 * Open the screens as the owner does (`guildbotics open`): ask an entry link,
 * and let the browser exchange its single-use code for the session cookie.
 */
export async function enter(page: Page, ctx: StackContext): Promise<void> {
  const response = await ownerApi(ctx, "/session/links", { method: "POST", body: "{}" });
  if (!response.ok) {
    throw new Error(`entry link: status ${response.status} ${await response.text()}`);
  }
  const { url } = (await response.json()) as { url: string };
  await page.goto(url);
  // The entry page replaces itself with the App once the session is taken up.
  await page.waitForURL((address) => address.pathname === "/");
}

/**
 * Every journey starts entered: each test's browser context gets a session of
 * its own, from the stack its project is named after (`playwright.config.ts`).
 */
export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    await enter(page, readStackContext(testInfo.project.name));
    await use(page);
  },
});
