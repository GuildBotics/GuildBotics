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
      expect(screen.getByRole("link", { name: "Branvil" })).toHaveAttribute("href", "/");
    },
  );
});
