import { afterEach, describe, expect, it } from "vitest";
import { createAppI18n } from "./i18n";

afterEach(() => localStorage.clear());

describe("application language instances", () => {
  it("keeps resources and persisted choices separate for each entry point", async () => {
    const make = (storageKey: string, text: string) =>
      createAppI18n({
        storageKey,
        resources: {
          en: { translation: { title: text } },
          ja: { translation: { title: `${text}-ja` } },
        },
      });
    const desktop = make("desktop.language", "Desktop");
    const other = make("other.language", "Other");
    await desktop.setAppLanguage("ja");
    expect(desktop.i18n.t("title")).toBe("Desktop-ja");
    expect(other.i18n.t("title")).toBe("Other");
    expect(localStorage.getItem("desktop.language")).toBe("ja");
    expect(localStorage.getItem("other.language")).toBeNull();
    expect(make("desktop.language", "Reloaded").i18n.t("title")).toBe("Reloaded-ja");
  });
});

it("shares the language across entry points while keeping their resources", async () => {
  const resources = {
    en: { translation: { title: "English" } },
    ja: { translation: { title: "日本語" } },
  };
  const first = createAppI18n({ resources });
  await first.setAppLanguage("ja");
  const second = createAppI18n({ resources });
  expect(second.i18n.language).toBe("ja");
  await second.i18n.changeLanguage("en");
  expect(createAppI18n({ resources }).i18n.language).toBe("en");
  expect(document.documentElement.lang).toBe("en");
});
