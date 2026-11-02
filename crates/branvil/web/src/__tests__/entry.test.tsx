import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { getCsrfToken } from "../api/client";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });

describe("entering without a sign-in screen", () => {
  it("exchanges the link's code once, drops it from the address bar, and keeps the CSRF value", async () => {
    const fake = fakeApi({
      "POST /api/v1/session": json(200, SESSION),
      "GET /api/v1/workspaces": page([]),
      "GET /api/v1/repos": page([{ path: "acme/demo" }]),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    renderApp("/-/enter#code-123");

    expect(
      await screen.findByRole("heading", { name: "No workspace yet" }),
    ).toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/session").map((r) => r.body)).toEqual([
      { code: "code-123" },
    ]);
    // The entry page does not wait for (or race) a session read.
    expect(fake.calls("GET", "/api/v1/session")).toHaveLength(0);
    expect(window.location.pathname).toBe("/");
    expect(window.location.hash).toBe("");
    expect(document.body.innerHTML).not.toContain("code-123");
    expect(getCsrfToken()).toBe("csrf-123");
  });

  it("says the link expired when the code is refused", async () => {
    fakeApi({
      "POST /api/v1/session": apiError(
        401,
        "unauthorized",
        "No valid token or session.",
      ),
    });
    renderApp("/-/enter#used");
    expect(
      await screen.findByText("This link has expired or was already used."),
    ).toBeInTheDocument();
    expect(screen.getByText("branvil open")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
  });

  it("keeps the link usable when the exchange fails for another reason", async () => {
    const fake = fakeApi({
      "POST /api/v1/session": [
        apiError(421, "misdirected", "This Host is not Branvil's."),
        json(200, SESSION),
      ],
      "GET /api/v1/workspaces": page([]),
      "GET /api/v1/repos": page([]),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    const user = userEvent.setup();
    renderApp("/-/enter#code-9");
    expect(
      await screen.findByText("This Host is not Branvil's."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("This link has expired or was already used."),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { name: "No workspace yet" }),
    ).toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/session").map((r) => r.body)).toEqual([
      { code: "code-9" },
      { code: "code-9" },
    ]);
  });

  it("shows the guidance, not an expiry, for a link without a code", async () => {
    const fake = fakeApi({});
    renderApp("/-/enter");
    expect(
      await screen.findByText("Open Branvil from the Hub machine with:"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("This link has expired or was already used."),
    ).not.toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/session")).toHaveLength(0);
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
      await screen.findByText("Open Branvil from the Hub machine with:"),
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
      await screen.findByText("Open Branvil from the Hub machine with:"),
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
