import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { applyChange } from "../query/events";
import { card } from "./fixtures";

function routes(extra: Parameters<typeof fakeApi>[0] = {}) {
  const workspace = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [workspace], next_cursor: null }),
    "GET /api/v1/workspaces/acme": json(200, workspace),
    "GET /api/v1/members": json(200, { items: [SESSION.me], next_cursor: null }),
    "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [card("1", "ready")], next_cursor: null }),
    ...extra,
  };
}

describe.each(["/acme/-/board", "/acme"])("ticket deletion on %s", (page) => {
  it("removes a card when another screen's deletion emits a ticket event", async () => {
    let deleted = false;
    const fake = fakeApi(routes({
      "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: deleted ? [] : [card("1", "ready")], next_cursor: null }),
    }));
    const { client } = renderApp(page);
    await screen.findByText("Title of 1");
    deleted = true;
    act(() => applyChange({ kind: "ticket", path: "acme/demo", id: "1" }, client));
    await waitFor(() => expect(screen.queryByText("Title of 1")).not.toBeInTheDocument());
    expect(fake.requests.filter((r) => r.method === "DELETE")).toHaveLength(0);
  });

  it("requires confirmation, sends DELETE once, and removes the card after refetching", async () => {
    let deleted = false;
    const fake = fakeApi(routes({
      "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: deleted ? [] : [card("1", "ready")], next_cursor: null }),
      "DELETE /api/v1/repos/acme/demo/tickets/1": () => { deleted = true; return json(204, null); },
    }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").at(-1)).toHaveTextContent("Delete");
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    expect(confirm).toHaveBeenCalledWith("Delete acme/demo#1 and all its comments? This cannot be undone.");
    expect(fake.calls("DELETE", "/api/v1/repos/acme/demo/tickets/1")).toHaveLength(0);
    expect(screen.getByText("Title of 1")).toBeInTheDocument();
    await user.click(within(menu).getByRole("menuitem", { name: "Delete" }));
    await waitFor(() => expect(screen.queryByText("Title of 1")).not.toBeInTheDocument());
    const calls = fake.calls("DELETE", "/api/v1/repos/acme/demo/tickets/1");
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
    confirm.mockRestore();
  });

  it.each([
    ["delete_has_children", "This ticket has child tickets. Remove their parent links before deleting it."],
    ["delete_blocks_tickets", "Other tickets are blocked by this ticket. Remove those links before deleting it."],
    ["delete_linked_pull", "This ticket is linked to a pull request. Remove the link before deleting it."],
    ["delete_upstream_issue", "This ticket is linked to an upstream issue. Remove the link before deleting it."],
    ["unavailable", "Storage busy."],
  ])("keeps the card and displays the refusal for %s", async (code, message) => {
    fakeApi(routes({ "DELETE /api/v1/repos/acme/demo/tickets/1": apiError(409, code, "Storage busy.") }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByText("Title of 1")).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("does not offer deletion to an agent", async () => {
    fakeApi(routes({ "GET /api/v1/session": json(200, { ...SESSION, me: { ...SESSION.me, kind: "agent" } }) }));
    const user = userEvent.setup();
    renderApp(page);
    await user.click(await screen.findByRole("button", { name: "Actions of acme/demo#1" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
  });
});
