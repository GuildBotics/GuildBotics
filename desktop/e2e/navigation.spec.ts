import { expect } from "@playwright/test";
import { test } from "./stack-context";

test("moves between both screens with one session and language preference", async ({ page }) => {
  await page.goto("/-/guildbotics/#/service");
  await page.getByRole("link", { name: "Branvil", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await page.getByRole("combobox", { name: "Display language", exact: true }).click();
  await page.getByRole("option", { name: "日本語" }).click();
  await page.getByRole("link", { name: "GuildBotics", exact: true }).click();
  await expect(page.getByRole("link", { name: "設定", exact: true })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "ja");
  await page.getByRole("link", { name: "Branvil", exact: true }).click();
  await expect(page.getByRole("link", { name: "設定", exact: true })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "ja");
  await page.getByRole("link", { name: "GuildBotics", exact: true }).click();
  await page.getByRole("combobox", { name: "表示言語", exact: true }).click();
  await page.getByRole("option", { name: "English", exact: true }).click();
  await page.getByRole("link", { name: "Branvil", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Main" })).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
});
