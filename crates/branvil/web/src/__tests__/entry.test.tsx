import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });

describe("entering through the host", () => {
  it.each(["en", "ja"] as const)("explains how to open the printed link in the same browser (%s)", async (language) => {
    fakeApi({
      "GET /api/v1/session": apiError(401, "unauthorized", "No valid token or session."),
    });
    renderApp("/", language);
    expect(await screen.findByText("guildbotics open --print")).toBeInTheDocument();
    expect(screen.getByText(language === "en" ? /Paste the printed URL into the address bar of this same browser/ : /表示されたURLを、この画面と同じブラウザのアドレスバーに貼り付けて/)).toBeInTheDocument();
  });

  it("shows how to open Branvil on every page without a session, even when the session read fails", async () => {
    fakeApi({
      "GET /api/v1/session": apiError(
        401,
        "unauthorized",
        "No valid token or session.",
      ),
    });
    renderApp("/acme/demo");
    expect(
      await screen.findByText("On the Hub machine, print an entry link with:"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("keeps the guidance and a retry when the session cannot be read", async () => {
    const fake = fakeApi({
      "GET /api/v1/session": [
        apiError(421, "misdirected", "This Host is not Branvil's."),
        json(200, SESSION),
      ],
      "GET /api/v1/workspaces": page([]),
      "GET /api/v1/repos": page([]),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    const user = userEvent.setup();
    renderApp("/");
    expect(
      await screen.findByText("On the Hub machine, print an entry link with:"),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("This Host is not Branvil's."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { name: "No workspace yet" }),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(
        fake.calls("GET", "/api/v1/session").length,
      ).toBeGreaterThanOrEqual(2),
    );
  });
});
