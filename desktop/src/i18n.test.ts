import { describe, expect, it } from "vitest";

import i18n, { getInitialAppLanguage, normalizeLanguage, setAppLanguage } from "./i18n";

describe("normalizeLanguage", () => {
  it("accepts exact supported language codes", () => {
    expect(normalizeLanguage("en")).toBe("en");
    expect(normalizeLanguage("ja")).toBe("ja");
  });

  it("normalizes locale strings to supported language codes", () => {
    expect(normalizeLanguage("en-US")).toBe("en");
    expect(normalizeLanguage("ja-JP")).toBe("ja");
  });

  it("returns null for unsupported or missing values", () => {
    expect(normalizeLanguage("fr-FR")).toBeNull();
    expect(normalizeLanguage("")).toBeNull();
    expect(normalizeLanguage(undefined)).toBeNull();
  });
});

describe("app language storage", () => {
  it("persists the selected application language", async () => {
    localStorage.clear();

    await setAppLanguage("ja");

    expect(getInitialAppLanguage()).toBe("ja");
    expect(i18n.language).toBe("ja");
  });
});
