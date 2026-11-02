// Entering the GUI the way the owner does: `branvil open --print` on the Hub
// machine prints a single-use link, and the browser opens it.
//
// Environment:
//   BRANVIL_E2E_BASE_URL  base URL of the running service
//   BRANVIL_E2E_BIN       the branvil binary
//   BRANVIL_E2E_DATA_DIR  the service's data directory (its secrets/owner.token is the owner's token)

import { execFileSync } from "node:child_process";
import { expect, type Page } from "@playwright/test";

export const entryEnv = {
  base: process.env.BRANVIL_E2E_BASE_URL,
  bin: process.env.BRANVIL_E2E_BIN,
  data: process.env.BRANVIL_E2E_DATA_DIR,
};

/** Opens a new GUI session of the owner, then `path`. */
export async function enter(page: Page, path = "/") {
  // The token comes from the data directory, never from this environment.
  const childEnv = { ...process.env };
  delete childEnv.BRANVIL_TOKEN;
  const link = execFileSync(
    entryEnv.bin as string,
    ["open", "--print", "--url", entryEnv.base as string, "--data-dir", entryEnv.data as string],
    { encoding: "utf8", env: childEnv },
  ).trim();
  await page.goto(link);
  await expect(page.getByRole("link", { name: /^(Settings|設定)$/ })).toBeVisible();
  expect(page.url()).not.toContain("#");
  if (path !== "/") await page.goto(path);
}
