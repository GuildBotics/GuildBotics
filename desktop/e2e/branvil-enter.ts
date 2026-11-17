import { expect, type Page } from "@playwright/test";
import { enter as enterHost, readStackContext } from "./stack-context";

export const entryEnv = {
  get base() {
    const ctx = readStackContext("branvil");
    return `http://${ctx.host}:${ctx.backendPort}`;
  },
};

/** Enter Branvil through the host's shared browser session. */
export async function enter(page: Page, path = "/") {
  await enterHost(page, readStackContext("branvil"));
  await page.goto(path);
  await expect(page.getByRole("link", { name: /^(Settings|設定)$/ })).toBeVisible();
  expect(page.url()).not.toContain("#");
}
