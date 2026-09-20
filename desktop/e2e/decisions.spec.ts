import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { readStackContext } from "./stack-context";
import { expect, test } from "@playwright/test";

// Credentials use the isolated stack's fake keychain. Assignment persistence
// and the section save go through the real API. No model call is needed.
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
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveCount(0);
  const row = card;
  await row.getByRole("combobox").nth(0).click();
  await page.getByRole("option", { name: "Jev", exact: true }).click();
  await expect(card.getByRole("combobox")).toHaveCount(1);
  await expect(
    card.getByText("Uses jev-latest (latest official release).", { exact: true }),
  ).toBeVisible();
  await card.getByLabel("Jev API key", { exact: true }).fill("synthetic-e2e-jev-key");
  await expect(card.getByRole("button", { name: /Save Jev|Check saved/ })).toHaveCount(0);
  const inputWidth = await card
    .getByLabel("Jev API key", { exact: true })
    .evaluate((el) => el.getBoundingClientRect().width);
  const cardWidth = await card.evaluate((el) => el.getBoundingClientRect().width);
  expect(inputWidth).toBeGreaterThan(cardWidth * 0.8);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByRole("combobox").nth(0)).toHaveValue("Jev");
  await expect(card.getByRole("combobox")).toHaveCount(1);
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveValue("");
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveAttribute(
    "placeholder",
    "••••••••••••",
  );
  // A save with a blank field must retain the stored credential.
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.reload();
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveAttribute(
    "placeholder",
    "••••••••••••",
  );
});
