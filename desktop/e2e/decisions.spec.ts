import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { readStackContext } from "./stack-context";
import { expect, test } from "@playwright/test";

// Credentials use the isolated stack's fake keychain. Assignment persistence
// goes through the real API; only the paid model invocation is mocked.
test("uses the common brain assignment for chat judgment and restores it after reload", async ({
  page,
}) => {
  // A nonempty existing mapping does not inherit newly added template features.
  const ctx = readStackContext("configured");
  writeFileSync(
    join(ctx.configDir, "intelligences", "brain_mapping.yml"),
    "default:\n  class: guildbotics.intelligences.brains.agno_agent.AgnoAgentDefaultBrain\n  args:\n    model: default\n",
  );
  await page.goto("/#/setup?section=intelligence&advanced=intelligence");
  const card = page.locator("#decision-settings");
  await expect(card.getByText("Chat judgment engine", { exact: true })).toBeVisible();
  await expect(card.getByRole("combobox")).toHaveCount(2);
  await expect(card.getByText("Not configured", { exact: true })).toBeVisible();
  await expect(card.getByRole("combobox").nth(0)).toHaveValue("");
  for (const title of ["LLM & Models", "AI CLI Tools"]) {
    expect(
      await page
        .getByText(title, { exact: true })
        .last()
        .evaluate(
          (node) =>
            !!(
              node.compareDocumentPosition(document.getElementById("decision-settings")!) &
              Node.DOCUMENT_POSITION_FOLLOWING
            ),
        ),
    ).toBe(true);
  }
  expect(
    await page
      .getByText("Environment declaration", { exact: true })
      .last()
      .evaluate(
        (node) =>
          !!(
            node.compareDocumentPosition(document.getElementById("decision-settings")!) &
            Node.DOCUMENT_POSITION_PRECEDING
          ),
      ),
  ).toBe(true);
  await card.getByLabel("Jev API key", { exact: true }).fill("synthetic-e2e-jev-key");
  await card.getByRole("button", { name: "Save Jev credentials", exact: true }).click();
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveValue("");
  const row = card;
  await row.getByRole("combobox").nth(0).click();
  await page.getByRole("option", { name: "Jev", exact: true }).click();
  await expect(card.getByRole("combobox")).toHaveCount(1);
  await expect(
    card.getByText("Uses jev-latest (latest official release).", { exact: true }),
  ).toBeVisible();
  await expect(card.getByText("Not configured", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Check saved chat judgment assignment", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByRole("combobox").nth(0)).toHaveValue("Jev");
  await expect(card.getByRole("combobox")).toHaveCount(1);
  await expect(card.getByText("Engine: Jev", { exact: true })).toBeVisible();
  await expect(card.getByText("Configured model: jev-latest", { exact: true })).toBeVisible();
  await page.route("**/intelligences/decisions/check", (route) => {
    expect(route.request().postDataJSON()).toEqual({ config: { brain: "chat_decision" } });
    return route.fulfill({ json: { state: "verified", model: "jev-1.13.0" } });
  });
  await card
    .getByRole("button", { name: "Check saved chat judgment assignment", exact: true })
    .click();
  await expect(card.getByRole("alert")).toContainText("Connection verified");
});
