import { pathToFileURL } from "node:url";

import { chromium } from "@playwright/test";

export async function verifyChromium(launcher = chromium) {
  let browser;
  try {
    browser = await launcher.launch({ headless: true });
    const page = await browser.newPage();
    await page.goto("data:text/plain,Chromium%20preflight");
  } catch (error) {
    throw new Error(
      [
        "E2E infrastructure failure: Chromium could not start in this environment.",
        "Playwright journeys were not started; this is not a GuildBotics journey failure.",
        String(error),
      ].join("\n"),
      { cause: error },
    );
  } finally {
    try {
      await browser?.close();
    } catch {
      // The preflight verdict is already decided; cleanup must not mask it.
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  verifyChromium().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
