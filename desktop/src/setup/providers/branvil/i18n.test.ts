import { describe, expect, it } from "vitest";

import i18n from "../../../i18n";

describe("Branvil reachability guidance", () => {
  it.each(["en", "ja"])("asks to check the running service in %s", (language) => {
    const t = i18n.getFixedT(language);
    expect(i18n.getFixedT(language)("setup.branvil.baseUrl")).toBe(
      language === "en" ? "Connection" : "接続先",
    );
    const diagnostic = t("overview.diagnosticChecks.branvil_reachable.description", {
      target: "http://127.0.0.1:7878",
    });
    expect(diagnostic).toContain("http://127.0.0.1:7878");
    expect(diagnostic).toBe(
      language === "en"
        ? "Branvil did not answer at http://127.0.0.1:7878. Check that Branvil is running."
        : "http://127.0.0.1:7878 の Branvil が応答しませんでした。Branvil が動いていることを確認してください。",
    );
  });
});
