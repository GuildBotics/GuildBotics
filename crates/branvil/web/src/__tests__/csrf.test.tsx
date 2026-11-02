import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { request } from "../api/client";
import { ErrorMessage, errorText } from "../components/common";
import { dictionaries, I18nProvider, type Translate } from "../i18n";
import { apiError, fakeApi, json } from "../test/fakeApi";
import { theme } from "../theme";
import { moveRefusal } from "../views/board/CardView";
import { card } from "./fixtures";

describe("CSRF recovery guidance", () => {
  it.each(["ja", "en"] as const)("asks for a page reload in %s after recovery fails", async (lang) => {
    fakeApi({
      "POST /api/v1/x": apiError(403, "csrf_mismatch", "CSRF mismatch."),
      "GET /api/v1/session": json(200, { csrf: "fresh" }),
    });
    const error: unknown = await request("POST", "/x").catch((err: unknown) => err);
    render(
      <MantineProvider theme={theme} env="test">
        <I18nProvider initial={lang}>
          <ErrorMessage error={error} />
        </I18nProvider>
      </MantineProvider>,
    );
    const guidance = dictionaries[lang]["common.sessionChanged"];
    expect(screen.getByRole("alert")).toHaveTextContent(guidance);
    expect(screen.queryByText(/CSRF|permission|権限/)).not.toBeInTheDocument();
    const t: Translate = (key) => dictionaries[lang][key];
    expect(errorText(error, t)).toBe(guidance);
    // Card moves embed the same localized guidance in their failure notice.
    expect(moveRefusal(error, card("1", "ready"), (key, params) => key === "board.moveFailed" ? String(params?.message) : t(key)).text).toBe(guidance);
  });
});
