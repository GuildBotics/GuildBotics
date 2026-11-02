import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Card } from "../api/types";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { card } from "./fixtures";

function routes(cards: () => Card[], extra: Parameters<typeof fakeApi>[0] = {}) {
  const workspace = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [workspace], next_cursor: null }),
    "GET /api/v1/workspaces/acme": json(200, workspace),
    "GET /api/v1/members": json(200, { items: [SESSION.me], next_cursor: null }),
    "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: cards(), next_cursor: null }),
    ...extra,
  };
}

const ancestor = { url: `${window.location.origin}/acme/demo/-/tickets/9`, repository: "acme/demo", key: "9", title: "Title of 9", state: "open" as const, state_reason: null };

describe.each(["/acme/-/board", "/acme"])("the interactive item of a card's menu on %s", (page) => {
  it("turns a ticket's interactive on, closes the menu, and shows the card's badge once read again", async () => {
    let interactive = false;
    const fake = fakeApi(routes(() => [card("1", "ready", { interactive })], {
      "PATCH /api/v1/repos/acme/demo/tickets/1": () => {
        interactive = true;
        return json(200, { ticket: card("1", "ready", { interactive }), state_changed: false });
      },
    }));
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Interactive" }));
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/1").map((r) => r.body)).toEqual([{ interactive: true }]);
    const article = screen.getByRole("article", { name: /^acme\/demo#1 / });
    expect(await within(article).findByText("Interactive")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Actions of acme/demo#1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Interactive" }));
    await waitFor(() => expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/1")).toHaveLength(2));
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/1")[1]?.body).toEqual({ interactive: false });
  });

  it("cannot turn off what a ticket inherits from its ancestor", async () => {
    fakeApi(routes(() => [card("1", "ready", { interactive_ancestor: ancestor })]));
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    expect(await screen.findByRole("menuitem", { name: "Interactive" })).toBeDisabled();
  });

  it("keeps the menu open with the refusal when the change is refused", async () => {
    fakeApi(routes(() => [card("1", "ready")], { "PATCH /api/v1/repos/acme/demo/tickets/1": apiError(503, "unavailable", "Storage busy.") }));
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Interactive" }));
    expect(await within(screen.getByRole("menu")).findByRole("alert")).toHaveTextContent("Storage busy.");
  });
});
