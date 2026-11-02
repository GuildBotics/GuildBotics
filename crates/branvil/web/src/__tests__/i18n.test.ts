import { describe, expect, it } from "vitest";
import { LANES } from "../api/types";
import { detectLanguage, dictionaries, format } from "../i18n";

describe("i18n", () => {
  it("has identical keys in Japanese and English", () => {
    const en = Object.keys(dictionaries.en).sort();
    const ja = Object.keys(dictionaries.ja).sort();
    expect(ja).toEqual(en);
  });

  it("has no empty messages and the same placeholders in both languages", () => {
    const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    for (const [key, value] of Object.entries(dictionaries.en)) {
      const ja = dictionaries.ja[key as keyof typeof dictionaries.ja];
      expect(value.trim(), key).not.toBe("");
      expect(ja.trim(), key).not.toBe("");
      expect(placeholders(ja), key).toEqual(placeholders(value));
    }
  });

  it("names every lane in Japanese", () => {
    expect(LANES.map((lane) => dictionaries.ja[`lane.${lane}`])).toEqual(["新規", "着手可", "作業中", "完了"]);
  });

  it("picks the language from the browser and interpolates", () => {
    expect(detectLanguage(["ja-JP", "en"])).toBe("ja");
    expect(detectLanguage(["fr", "en-US"])).toBe("en");
    expect(detectLanguage(["fr"])).toBe("en");
    expect(format("{key} moved to {lane}.", { key: "acme/demo#1", lane: "Ready" })).toBe("acme/demo#1 moved to Ready.");
  });
});
