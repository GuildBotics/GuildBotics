import { expect, test } from "@playwright/test";

// Credentials use the isolated stack's fake keychain. Assignment persistence
// goes through the real API; only the paid model invocation is mocked.
test("uses the common brain assignment for chat judgment and restores it after reload", async ({
  page,
}) => {
  await page.goto("/#/setup?section=intelligence&advanced=intelligence");
  const card = page.locator("#decision-settings");
  await expect(card.getByText("Chat judgment engine", { exact: true })).toBeVisible();
  await expect(card.getByRole("combobox")).toHaveCount(0);
  await card.getByLabel("Jev API key", { exact: true }).fill("synthetic-e2e-jev-key");
  await card.getByRole("button", { name: "Save Jev credentials", exact: true }).click();
  await expect(card.getByLabel("Jev API key", { exact: true })).toHaveValue("");
  const row = page
    .locator(".mantine-Group-root")
    .filter({ has: page.locator('input[value="chat_decision"]') })
    .last();
  await row.getByRole("combobox").nth(0).click();
  await page.getByRole("option", { name: "Jev", exact: true }).click();
  await row.getByRole("combobox").nth(1).click();
  await page.getByRole("option", { name: "jev-1.13.0", exact: true }).click();
  await expect(
    card.getByRole("button", { name: "Check saved chat judgment assignment", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.reload();
  await expect(row.getByRole("combobox").nth(0)).toHaveValue("Jev");
  await expect(row.getByRole("combobox").nth(1)).toHaveValue("jev-1.13.0");
  await page.route("**/intelligences/decisions/check", (route) => {
    expect(route.request().postDataJSON()).toEqual({ config: { brain: "chat_decision" } });
    return route.fulfill({ json: { state: "verified", model: "jev-1.13.0" } });
  });
  await card
    .getByRole("button", { name: "Check saved chat judgment assignment", exact: true })
    .click();
  await expect(card.getByRole("alert")).toContainText("Connection verified");
});
