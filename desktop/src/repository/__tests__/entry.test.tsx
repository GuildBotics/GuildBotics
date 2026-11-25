import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });

describe("entering through the host", () => {
  it.each(["en", "ja"] as const)(
    "explains how to open the printed link in the same browser (%s)",
    async (language) => {
      fakeApi({
        "GET /-/guildbotics/api/session": apiError(
          401,
          "unauthorized",
          "No valid token or session.",
        ),
      });
      renderApp("/", language);
      expect(await screen.findByText("ssh <server> guildbotics open --print")).toBeInTheDocument();
      expect(
        screen.getByText(
          language === "en"
            ? /paste the printed link into this browser's address bar/
            : /表示されたリンクをこのブラウザのアドレスバーに貼り付けて/,
        ),
      ).toBeInTheDocument();
    },
  );

  it("shows the same entry instructions on a repository deep link", async () => {
    fakeApi({
      "GET /-/guildbotics/api/session": apiError(401, "unauthorized", "No valid token or session."),
    });
    renderApp("/workspaces/acme/repos/demo");
    expect(await screen.findByText("guildbotics open", { exact: true })).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("keeps the guidance and a retry when the session cannot be read", async () => {
    const fake = fakeApi({
      "GET /-/guildbotics/api/session": [
        apiError(421, "misdirected", "This Host is not Branvil's."),
        json(200, SESSION),
      ],
      "GET /api/v1/workspaces": page([]),
      "GET /api/v1/repos": page([]),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    const user = userEvent.setup();
    renderApp("/workspaces");
    expect(await screen.findByRole("dialog")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "No workspace yet" })).toBeInTheDocument();
    await waitFor(() =>
      expect(fake.calls("GET", "/-/guildbotics/api/session").length).toBeGreaterThanOrEqual(2),
    );
  });
});
