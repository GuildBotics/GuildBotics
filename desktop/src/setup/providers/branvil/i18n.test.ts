import { describe, expect, it } from "vitest";

import i18n from "../../../i18n";

describe("Branvil workspace guidance", () => {
  it.each(["en", "ja"])("names the workspace to check in %s", (language) => {
    const t = i18n.getFixedT(language);
    expect(t("overview.diagnosticChecks.branvil_workspace.description", { target: "acme" })).toBe(
      language === "en"
        ? "Check that the workspace acme exists in GuildBotics."
        : "ワークスペース acme が GuildBotics にあるか確認してください。",
    );
  });
});
