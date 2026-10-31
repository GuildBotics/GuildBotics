import { describe, expect, it } from "vitest";

import i18n from "../../../i18n";

describe("Branvil reachability guidance", () => {
  it.each(["en", "ja"])("points at the address and its setting in %s", (language) => {
    const t = i18n.getFixedT(language);
    expect(i18n.exists("setup.branvil.baseUrlHint", { lng: language })).toBe(true);
    const diagnostic = t("overview.diagnosticChecks.branvil_reachable.description", {
      target: "http://127.0.0.1:7878",
    });
    expect(diagnostic).toContain("http://127.0.0.1:7878");
    expect(diagnostic).toContain(t("setup.branvil.baseUrl"));
  });
});
