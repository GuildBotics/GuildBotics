import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import i18n from "../../../i18n";
import { BranvilNavItem } from "./BranvilNavItem";

describe("Branvil sidebar entry", () => {
  it.each(["en", "ja"])(
    "links to the shared origin without provider configuration in %s",
    async (language) => {
      await i18n.changeLanguage(language);
      render(<BranvilNavItem />);
      expect(i18n.t("setup.branvil.connection.hint")).toBe(
        language === "en"
          ? "Connecting opens Branvil's consent screen in this browser. Allow GuildBotics to manage Branvil there."
          : "Branvil に接続すると、このブラウザで Branvil の同意画面が開きます。GuildBotics に Branvil の管理を許可してください。",
      );
      expect(screen.getByRole("link", { name: "Branvil" })).toHaveAttribute("href", "/");
    },
  );
});
