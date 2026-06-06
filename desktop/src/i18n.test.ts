import { afterEach, beforeEach, describe, expect, it } from "vitest";

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

describe("getInitialAppLanguage", () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(window.navigator, "language");

  function mockNavigatorLanguage(value: string) {
    Object.defineProperty(window.navigator, "language", {
      configurable: true,
      get: () => value,
    });
  }

  beforeEach(() => {
    localStorage.clear();
    mockNavigatorLanguage("en-US");
  });

  afterEach(() => {
    if (originalDescriptor) {
      Object.defineProperty(window.navigator, "language", originalDescriptor);
    }
    localStorage.clear();
  });

  it("uses a valid stored language", () => {
    localStorage.setItem("guildbotics.appLanguage", "ja");
    expect(getInitialAppLanguage()).toBe("ja");
  });

  it("falls back to navigator language when storage holds an invalid value", () => {
    localStorage.setItem("guildbotics.appLanguage", "fr");
    mockNavigatorLanguage("ja-JP");
    expect(getInitialAppLanguage()).toBe("ja");
  });

  it("falls back to en when both storage and navigator are unsupported", () => {
    localStorage.setItem("guildbotics.appLanguage", "fr");
    mockNavigatorLanguage("fr-FR");
    expect(getInitialAppLanguage()).toBe("en");
  });

  it("uses navigator language when storage is empty", () => {
    mockNavigatorLanguage("ja");
    expect(getInitialAppLanguage()).toBe("ja");
  });
});

describe("app language storage", () => {
  afterEach(async () => {
    localStorage.clear();
    await setAppLanguage("en");
  });

  it("persists the selected application language", async () => {
    localStorage.clear();

    await setAppLanguage("ja");

    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("ja");
    expect(getInitialAppLanguage()).toBe("ja");
    expect(i18n.language).toBe("ja");
  });

  it("updates both localStorage and i18next state when switching back", async () => {
    await setAppLanguage("ja");
    await setAppLanguage("en");

    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("en");
    expect(i18n.language).toBe("en");
  });
});

describe("i18n resources", () => {
  function collectKeys(node: unknown, prefix = ""): string[] {
    if (node && typeof node === "object" && !Array.isArray(node)) {
      return Object.entries(node as Record<string, unknown>).flatMap(([key, value]) =>
        collectKeys(value, prefix ? `${prefix}.${key}` : key),
      );
    }
    return [prefix];
  }

  it("has identical translation keys between en and ja", () => {
    const en = i18n.getResourceBundle("en", "translation") as Record<string, unknown>;
    const ja = i18n.getResourceBundle("ja", "translation") as Record<string, unknown>;

    const enKeys = collectKeys(en).sort();
    const jaKeys = collectKeys(ja).sort();

    const missingInJa = enKeys.filter((key) => !jaKeys.includes(key));
    const missingInEn = jaKeys.filter((key) => !enKeys.includes(key));

    expect(missingInJa).toEqual([]);
    expect(missingInEn).toEqual([]);
  });
});
