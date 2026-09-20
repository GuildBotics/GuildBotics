import { expect, test } from "@playwright/test";

// Credentials are synthetic and stored by the isolated configured stack's
// fake keychain. Only the connection probe is mocked; saving uses the real API.
test("registers Jev while disabled, saves its model, and restores it after reload", async ({
  page,
}) => {
  await page.goto("/#/setup?section=intelligence&advanced=intelligence");
  const card = page.locator("#decision-settings");
  await expect(card.getByText("Chat judgment engine", { exact: true })).toBeVisible();
  await card.getByRole("combobox", { name: "Engine / provider" }).click();
  await expect(page.getByRole("option", { name: "Jev", exact: true })).toHaveAttribute(
    "data-combobox-disabled",
    "true",
  );
  await page.keyboard.press("Escape");
  await card.getByLabel("Jev API key", { exact: true }).fill("synthetic-e2e-jev-key");
  await card.getByRole("button", { name: "Save Jev credentials", exact: true }).click();
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveValue("");
  await card.getByRole("combobox", { name: "Engine / provider" }).click();
  await page.getByRole("option", { name: "Jev", exact: true }).click();
  await expect(card.getByRole("combobox", { name: "Model ID", exact: true })).toHaveValue(
    "jev-1.13.0",
  );
  await page.route("**/intelligences/decisions/check", (route) =>
    route.fulfill({
      json: {
        status: {
          available: true,
          state: "verified",
          reason: "Verified both question types",
          recovery: "credentials",
        },
        models: ["jev-latest", "jev-preview"],
      },
    }),
  );
  await card
    .getByRole("button", { name: "Check connection / refresh models", exact: true })
    .click();
  await expect(card.getByRole("alert")).toContainText("Connection verified");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.reload();
  await expect(card.getByRole("combobox", { name: "Engine / provider" })).toHaveValue("Jev");
  await expect(card.getByRole("combobox", { name: "Model ID", exact: true })).toHaveValue(
    "jev-1.13.0",
  );
});
