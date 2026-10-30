import { describe, expect, it } from "vitest";

import i18n from "../../../i18n";

describe("Branvil SSH forwarding guidance", () => {
  it.each(["en", "ja"])("explains setup and status in %s", (language) => {
    const t = i18n.getFixedT(language);
    for (const key of [
      "setup.branvil.baseUrlHint",
      "overview.diagnosticChecks.branvil_reachable.description",
    ]) {
      expect(i18n.exists(key, { lng: language })).toBe(true);
      const message = t(key, { target: "http://127.0.0.1:7878" });
      expect(message).toContain("scripts/hub-forward.sh");
      expect(message).toContain("hub-forward.ps1");
    }
    const diagnostic = t("overview.diagnosticChecks.branvil_reachable.description", {
      target: "http://127.0.0.1:7878",
    });
    expect(diagnostic).toContain("http://127.0.0.1:7878");
    expect(diagnostic).toContain("status");
    expect(diagnostic).toContain("-ExecutionPolicy Bypass");
  });
});
