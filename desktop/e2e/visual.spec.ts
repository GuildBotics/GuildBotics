import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect } from "@playwright/test";
import { test } from "./stack-context";

// Opt-in review evidence, using the same real, isolated backend as the journeys.
const phase = process.env.GUILDBOTICS_SCREENSHOT_PHASE;
test("capture the desktop presentation for review", async ({ page }) => {
  test.skip(!phase, "Set GUILDBOTICS_SCREENSHOT_PHASE to capture review evidence");
  const directory = resolve("../docs/screenshots/117", phase!);
  mkdirSync(directory, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    for (const route of ["service", "commands", "diagnostics", "setup"]) {
      await page.goto(route === "diagnostics" ? "/#/diagnostics?tab=memory" : `/#/${route}`);
      await expect(page.locator("main")).toBeVisible();
      await page.waitForLoadState("networkidle");
      await page.addStyleTag({
        content: '[aria-label="System health alerts"] { display: none !important; }',
      });
      if (route === "commands") {
        await expect(page.locator(".cm-content")).toBeVisible();
        await expect(page.getByText("Cannot run this command", { exact: true })).toBeVisible();
      }
      await page.screenshot({
        path: `${directory}/${route}-${scheme}.png`,
        fullPage: true,
        animations: "disabled",
        caret: "hide",
        // The isolated backend has a fresh temporary workspace on each run.
        mask: [page.getByLabel("Workspace", { exact: true })],
      });
    }
  }
});
