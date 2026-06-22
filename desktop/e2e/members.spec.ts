import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ②: add a team member through the UI against the REAL backend.
//
// This stack ("members") is seeded exactly like the configured stack — one
// active member, `Local Agent (local-agent)` — but kept isolated so mutating the
// member list never perturbs the service / commands journeys. The test opens the
// Members section of the configured Settings screen, adds a SECOND active member
// through the real `POST /config/members` wire, then asserts BOTH members render
// in the reloaded list and that the backend persisted the new member on disk.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  configDir: string;
  memberId: string | null;
  seeded: boolean;
};

function readMembersContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-members.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("adds a second member through the UI and persists it to the backend", async ({ page }) => {
  const ctx = readMembersContext();
  expect(ctx.seeded).toBe(true);

  // The Members section of the configured Settings screen (deep link, mirroring
  // SetupPage.test.tsx `?section=members`).
  await page.goto("/#/setup?section=members");
  await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

  // The seeded member is already listed.
  await expect(page.getByText("Local Agent (local-agent)")).toBeVisible({ timeout: 30_000 });

  // With a member already configured the add form is collapsed behind the
  // "Add new member" toggle; reveal it, then fill the required Basic fields.
  await page.getByRole("button", { name: "Add new member" }).click();
  await page.getByLabel("Member ID").fill("local-agent-2");
  await page.getByLabel("Display name").fill("Second Agent");
  await page.getByRole("textbox", { name: "Roles" }).click();
  await page.getByRole("option", { name: "product" }).click();
  await page.getByRole("button", { name: "Add member" }).click();

  // The reloaded list (driven by the real /team response) now shows BOTH members.
  await expect(page.getByText("Second Agent (local-agent-2)")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Local Agent (local-agent)")).toBeVisible();

  // The REAL backend wrote the new member's person.yml under the temp workspace.
  const personFile = join(ctx.configDir, "team", "members", "local-agent-2", "person.yml");
  expect(existsSync(personFile)).toBe(true);
  const personYaml = readFileSync(personFile, "utf-8");
  expect(personYaml).toContain("Second Agent");
});
