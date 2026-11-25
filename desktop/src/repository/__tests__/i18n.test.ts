import i18n from "../../i18n";
import { describe, expect, it } from "vitest";
import { LANES } from "../api/types";
import { en } from "../i18n/en";
import { ja } from "../i18n/ja";
import { shared } from "../../sharedLocales";

const dictionaries = { en, ja };

describe.each([
  { namespace: "repository", ...dictionaries },
  { namespace: "shared", ...shared },
])("$namespace translations", ({ en, ja }) => {
  it("has identical keys in Japanese and English", () => {
    expect(Object.keys(ja).sort()).toEqual(Object.keys(en).sort());
  });

  it("has no empty messages and the same placeholders in both languages", () => {
    const placeholders = (message: string) =>
      [...message.matchAll(/\{\{\s*-?\s*([^,}]+?)(?:,[^}]+)?\s*\}\}/g)]
        .map((match) => match[1]?.trim())
        .sort();
    const japanese: Record<string, string> = ja;
    for (const [key, value] of Object.entries(en)) {
      const translated = japanese[key] ?? "";
      expect(value.trim(), key).not.toBe("");
      expect(translated.trim(), key).not.toBe("");
      expect(placeholders(translated), key).toEqual(placeholders(value));
    }
  });
});

describe("i18n", () => {
  it("describes independent landing without a gate or queue", () => {
    for (const words of Object.values(dictionaries)) {
      for (const key of [
        "settings.autoLandHelp",
        "settings.autoLandHelpUpstream",
        "notifications.help",
        "landing.missing",
      ] as const) {
        expect(words[key]).not.toMatch(/gate|queue|検問|列/i);
      }
    }
    expect(dictionaries.ja["notifications.help"]).toContain("PR ごとに 1 回");
    expect(dictionaries.en["notifications.help"]).toContain("once per PR");
    expect(dictionaries.ja["notifications.help"]).toContain("修復チケット");
    expect(dictionaries.en["notifications.help"]).toContain("repair ticket");
    expect(dictionaries.ja["notifications.help"]).toContain("作業が人の対応を待って止まった");
    expect(dictionaries.en["notifications.help"]).toContain("member work stops");
  });

  it("describes automatic responders as agent PR authors in both languages", () => {
    expect(dictionaries.ja["settings.responderHelp"]).toContain("AI メンバー（agent）の PR");
    expect(dictionaries.en["settings.responderHelp"]).toContain("agent PR");
    expect(dictionaries.ja["settings.responderHelp"]).toContain("在籍中");
    expect(dictionaries.en["settings.responderHelp"]).toContain("active");
    expect(dictionaries.ja["settings.responderAuto"]).toBe("自動");
    expect(dictionaries.en["settings.responderAuto"]).toBe("Automatic");
  });

  it("names every lane in Japanese", () => {
    expect(LANES.map((lane) => dictionaries.ja[`lane.${lane}`])).toEqual([
      "新規",
      "着手可",
      "作業中",
      "完了",
    ]);
  });

  it("explains waits and the next pickup in Japanese", () => {
    const t = i18n.getFixedT("ja", "repository");
    expect(
      t("pickup.waitFor", { ticket: "acme/demo#237" }) +
        t("pickup.waitDetails", { assignee: "aiko", lane: t("lane.in_progress") }),
    ).toBe("acme/demo#237 が閉じるまで（aiko・作業中）");
    expect(t("pickup.waitHint", { tickets: "acme/demo#237" })).toBe(
      "acme/demo#237 が閉じるまで始まらず、その間は始められる後ろのチケットが先に始まる",
    );
    expect(t("pickup.waitFor", { ticket: "other/repo#M" })).toBe("other/repo#M が閉じるまで");
    expect(
      i18n.getFixedT("en", "repository")("pickup.waitHint", {
        tickets: "acme/demo#W; acme/demo#K",
      }),
    ).toBe(
      "Will not start until all of these tickets are closed: acme/demo#W; acme/demo#K; ready tickets further down can start first in the meantime",
    );
    expect(t("pickup.next")).toBe("次に始まる");
  });
});
