import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Card } from "../api/types";
import { href } from "../router/routes";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose, pickFromMenu } from "../test/mantine";
import { card } from "./fixtures";

const WORKSPACE = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };

const repo = (path: string, labels: string[]) => ({
  path,
  default_branch: "main",
  clone_url: "",
  url: "",
  protected_branches: ["main"],
  required_checks: [],
  labels,
  upstream: null,
  revision: 1,
});

const MEMBERS = {
  items: [
    { name: "owner", display_name: "Owner", kind: "owner", accounts: [], reviewers: [] },
    { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] },
    { name: "ci", display_name: "", kind: "runner", accounts: [], reviewers: [] },
  ],
  next_cursor: null,
};

function boardRoutes(extra: Record<string, Parameters<typeof fakeApi>[0][string]> = {}) {
  const origin = window.location.origin;
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [WORKSPACE], next_cursor: null }),
    "GET /api/v1/workspaces/acme": json(200, WORKSPACE),
    "GET /api/v1/members": json(200, MEMBERS),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/repos": json(200, {
      items: [repo("acme/demo", ["bug"]), repo("acme/web", ["ui"]), repo("beta/x", [])],
      next_cursor: null,
    }),
    // Two pages: the board follows next_cursor.
    "GET /api/v1/workspaces/acme/board/cards": (req: { search: string }) =>
      req.search.includes("cursor=page2")
        ? json(200, { items: [card("3", "in_progress")], next_cursor: null })
        : json(200, {
            items: [
              card("1", "new", {
                labels: ["bug"],
                assignee: { name: "aiko", assigned_at: "2026-10-14T10:00:00.000Z" },
                pull_requests: [{ url: `${origin}/acme/demo/-/pulls/3`, label: "!3", title: "Fix", state: "open", merged: false, updated_at: "" }],
              }),
              // A board holds tickets of the repositories of its workspace.
              card("2", "ready", { repository: "acme/web" }),
            ],
            next_cursor: "page2",
          }),
    "GET /api/v1/repos/acme/demo/pulls/3/checks": json(200, CHECKS),
    ...extra,
  };
}

const CHECKS = {
  head_sha: "a",
  base_sha: "b",
  behind_by: 0,
  checks: [{ name: "test", status: "completed", conclusion: "failure", attempt_id: "att_1", attempt: 1 }],
  checks_expected: true,
  base_checks: 1,
};

const lane = (name: RegExp) => screen.getByRole("region", { name });

/** How often each card rendered since the last reset (a card renders its key link twice per render). */
function cardRenders() {
  const spy = vi.spyOn(href, "ticket");
  return {
    of: (key: string) => spy.mock.calls.filter(([, k]) => k === key).length,
    reset: () => spy.mockClear(),
  };
}

describe("kanban board", () => {
  it("renders lanes, cards from every page, PR/CI summary, and empty lanes", async () => {
    const fake = fakeApi(boardRoutes());
    renderApp("/acme/-/board");

    expect(await within(await screen.findByRole("region", { name: /^New/ })).findByText("Title of 1")).toBeInTheDocument();
    expect(within(lane(/^Ready/)).getByText("Title of 2")).toBeInTheDocument();
    expect(within(lane(/^In progress/)).getByText("Title of 3")).toBeInTheDocument();
    expect(within(lane(/^Done/)).getByText("No cards in this lane.")).toBeInTheDocument();
    expect(await screen.findByText(/CI: failing/)).toBeInTheDocument();
    const [first, second] = fake.calls("GET", "/api/v1/workspaces/acme/board/cards");
    expect(first?.search).toContain("state=open");
    expect(first?.search).toContain("comments=none");
    expect(second?.search).toContain("cursor=page2");
  });

  it("opens a ticket in a drawer over the board at the board's URL, with the ticket's URL to copy; a ticket linked there replaces it", async () => {
    const ticket = (key: string, extra: Partial<Card> = {}) => ({
      [`GET /api/v1/repos/acme/demo/tickets/${key}`]: json(200, card(key, "new", extra)),
      [`GET /api/v1/repos/acme/demo/tickets/${key}/comments`]: json(200, { items: [{
        id: `cmt_${key}`, author: "comment-agent", body: "Drawer comment", meta: null, created_at: "2026-10-14T10:00:00.000Z", reactions: [], url: "",
      }], next_cursor: null }),
      [`GET /api/v1/repos/acme/demo/tickets/${key}/pulls`]: json(200, { items: [], next_cursor: null }),
    });
    const three = card("3", "in_progress");
    const blocker = { url: three.url, repository: "acme/demo", key: "3", title: three.title, state: "open" as const, state_reason: null };
    fakeApi(boardRoutes({ "GET /api/v1/repos/acme/demo": json(200, repo("acme/demo", ["bug"])), ...ticket("1", { blocked_by: [blocker] }), ...ticket("3") }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");

    // A click with a modifier opens the ticket's page as a link does, not the drawer.
    fireEvent.click(await screen.findByRole("link", { name: "Title of 1" }), { ctrlKey: true });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Title of 1" }));
    const drawer = await screen.findByRole("dialog", { name: "Ticket" });
    expect(await within(drawer).findByRole("heading", { level: 2 })).toHaveTextContent("#1 Title of 1");
    expect(window.location.pathname).toBe("/acme/-/board");
    // The ticket's page, not the board, has no link back to the board.
    expect(within(drawer).queryByRole("link", { name: "Task board" })).not.toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "Copy the ticket's URL" }));
    expect(await navigator.clipboard.readText()).toBe(`${window.location.origin}/acme/demo/-/tickets/1`);
    await user.click(await within(drawer).findByRole("button", { name: "Copy the comment's URL" }));
    expect(await navigator.clipboard.readText()).toBe(`${window.location.origin}/acme/demo/-/tickets/1#cmt_1`);
    expect(within(drawer).getByTitle("Link to this comment")).toHaveAttribute("href", "/acme/demo/-/tickets/1#cmt_1");

    await user.click(within(drawer).getByRole("link", { name: /Title of 3/ }));
    expect(await within(drawer).findByRole("heading", { level: 2, name: /#3 Title of 3/ })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/acme/-/board");
    // The link that opened it is gone: the focus stays in the drawer.
    expect(drawer).toHaveFocus();

    // Escape in a field being typed in is the field's: the title's edit is cancelled, the drawer stays.
    await user.click(within(drawer).getByRole("heading", { level: 2, name: /#3 Title of 3/ }));
    await user.keyboard("{Escape}");
    expect(within(drawer).queryByRole("textbox", { name: "Title" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Ticket" })).toBeInTheDocument();
    // So is Escape in an open dropdown: it closes only the dropdown.
    await user.click(within(drawer).getByRole("combobox", { name: "Lane" }));
    expect(await screen.findByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("listbox")).not.toBeInTheDocument());
    expect(screen.getByRole("dialog", { name: "Ticket" })).toBeInTheDocument();
    // Elsewhere it closes the drawer, as its button does: a label's chip is not typed in.
    act(() => within(drawer).getByRole("checkbox", { name: "bug" }).focus());
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("link", { name: "Title of 1" }));
    await user.click(await within(await screen.findByRole("dialog", { name: "Ticket" })).findByRole("button", { name: "Close the drawer" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(lane(/New/)).toBeInTheDocument();
  });

  it("re-renders no card when a check answers or a drag moves over the board", async () => {
    let answer: (a: { status: number; body: unknown }) => void = () => {};
    fakeApi(
      boardRoutes({
        "GET /api/v1/repos/acme/demo/pulls/3/checks": () => new Promise((resolve) => (answer = resolve)),
      }),
    );
    const renders = cardRenders();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 3");
    // The members (for avatars) arrive once; after that nothing re-renders the cards.
    await waitFor(() => expect(screen.getByRole("article", { name: /^acme\/demo#1 / })).toHaveTextContent("aiko"));
    renders.reset();

    answer(json(200, CHECKS));
    expect(await screen.findByText(/CI: failing/)).toBeInTheDocument();
    // Dragging acme/web#2 over acme/demo#1 moves the drop marker: the board re-renders, its cards do not.
    const item = (ref: string) => screen.getByRole("article", { name: new RegExp(`^${ref} `) }).closest("li") as HTMLElement;
    const dataTransfer = { setData: () => {}, getData: () => "", effectAllowed: "" };
    fireEvent.dragStart(item("acme/web#2"), { dataTransfer });
    fireEvent.dragOver(item("acme/demo#1"), { dataTransfer, clientY: 10 });
    await waitFor(() => expect(item("acme/demo#1")).toHaveClass("insert-before", { exact: false }));
    expect(["1", "2", "3"].map(renders.of)).toEqual([0, 0, 0]);
  });

  it("mounts a card's assignee menu only once asked for, and assigns from it", async () => {
    const fake = fakeApi(boardRoutes({ "PUT /api/v1/repos/acme/web/tickets/2/assignee": json(200, {}) }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 2");
    expect(screen.queryAllByRole("menu")).toHaveLength(0);

    // The assignee offers the owner and agents, not runners.
    await user.click(screen.getByRole("button", { name: "Assignee of acme/web#2: Unassigned — click to change" }));
    expect(await screen.findByRole("menuitem", { name: "owner" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "ci" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "aiko" }));

    await waitFor(() => expect(fake.calls("PUT", "/api/v1/repos/acme/web/tickets/2/assignee")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/repos/acme/web/tickets/2/assignee")[0]?.body).toEqual({ name: "aiko" });
  });

  it("shows a card's one assignee and clears it", async () => {
    const fake = fakeApi(boardRoutes({ "DELETE /api/v1/repos/acme/demo/tickets/1/assignee": json(200, {}) }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    const card1 = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(card1).getAllByText("aiko", { selector: ".assignee" })).toHaveLength(1);

    await pickFromMenu(user, "Assignee of acme/demo#1: aiko — click to change", "No one");

    await waitFor(() => expect(fake.calls("DELETE", "/api/v1/repos/acme/demo/tickets/1/assignee")).toHaveLength(1));
  });

  it("offers a card's lanes and its place in the lane in its menu, not its assignee", async () => {
    fakeApi(boardRoutes());
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await user.click(screen.getByRole("button", { name: "Actions of acme/demo#1" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual(["New", "Ready", "In progress", "Done", "Up", "Down", "Interactive", "Copy URL", "Delete"]);

    await user.click(within(menu).getByRole("menuitem", { name: "Copy URL" }));
    expect(await navigator.clipboard.readText()).toBe(`${window.location.origin}/acme/demo/-/tickets/1`);
    // The item says it copied and stays. It does not close on the click.
    expect(await within(menu).findByRole("menuitem", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("leaves the copy item unchanged when the clipboard refuses", async () => {
    fakeApi(boardRoutes());
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValueOnce(new Error("denied"));

    await user.click(screen.getByRole("button", { name: "Actions of acme/demo#1" }));
    const menu = await screen.findByRole("menu");
    await user.click(within(menu).getByRole("menuitem", { name: "Copy URL" }));

    await act(() => new Promise((resolve) => setTimeout(resolve, 0)));
    expect(within(menu).getByRole("menuitem", { name: "Copy URL" })).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Copied" })).not.toBeInTheDocument();
    // Longer than the success label stays up: a refusal must not close the menu later.
    await act(() => new Promise((resolve) => setTimeout(resolve, 1_700)));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "Copied" })).not.toBeInTheDocument();
  });

  it("moves a card to another lane from its menu (no drag needed)", async () => {
    const fake = fakeApi(boardRoutes({ "PUT /api/v1/repos/acme/demo/tickets/1/board": json(200, {}) }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await pickFromMenu(user, "Actions of acme/demo#1", "Ready");

    await waitFor(() => expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/1/board")).toHaveLength(1));
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/1/board")[0]?.body).toEqual({
      lane: "ready",
      after: "acme/web#2",
    });
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/1/board")[0]?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
    expect(await screen.findByText("acme/demo#1 moved to Ready.")).toBeInTheDocument();
  });

  it("asks for closed tickets too when the user includes them, and remembers that on this browser", async () => {
    const fake = fakeApi(boardRoutes());
    const user = userEvent.setup();
    const { unmount } = renderApp("/acme/-/board");
    await screen.findByText("Title of 1");
    expect(screen.getByLabelText("Include closed tickets")).not.toBeChecked();
    await user.click(screen.getByLabelText("Include closed tickets"));
    await waitFor(() => expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards").some((r) => r.search.includes("state=all"))).toBe(true));

    // Shown again (a reload, another workspace), the board keeps the choice.
    unmount();
    const again = renderApp("/acme/-/board");
    expect(await screen.findByLabelText("Include closed tickets")).toBeChecked();
    await user.click(screen.getByLabelText("Include closed tickets"));
    again.unmount();
    renderApp("/acme/-/board");
    expect(await screen.findByLabelText("Include closed tickets")).not.toBeChecked();
  });

  it("filters loaded cards by assignee, label, and text", async () => {
    fakeApi(boardRoutes());
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await choose(user, "Assignee", "aiko");
    expect(screen.queryByText("Title of 2")).not.toBeInTheDocument();
    expect(within(lane(/^Ready/)).getByText("No cards match the filter.")).toBeInTheDocument();
    await choose(user, "Assignee", "Unassigned");
    expect(screen.queryByText("Title of 1")).not.toBeInTheDocument();
    expect(screen.getByText("Title of 2")).toBeInTheDocument();
    await choose(user, "Assignee", "Assignee: Any");
    await choose(user, "Label", "bug");
    expect(screen.getByText("Title of 1")).toBeInTheDocument();
    expect(screen.queryByText("Title of 2")).not.toBeInTheDocument();
    await choose(user, "Label", "Label: Any");
    await user.type(screen.getByRole("textbox", { name: "Text" }), "acme/demo#3");
    expect(screen.queryByText("Title of 1")).not.toBeInTheDocument();
    expect(screen.getByText("Title of 3")).toBeInTheDocument();
  });

  it("reverts a failed move and says so", async () => {
    fakeApi(boardRoutes({ "PUT /api/v1/repos/acme/demo/tickets/1/board": apiError(503, "unavailable", "Storage busy.") }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await pickFromMenu(user, "Actions of acme/demo#1", "Done");

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not move acme/demo#1; it is back where it was. Storage busy.");
    await waitFor(() => expect(within(lane(/^New/)).getByText("Title of 1")).toBeInTheDocument());
    expect(within(lane(/^Done/)).queryByText("Title of 1")).not.toBeInTheDocument();
  });

  it("marks a card blocked by an open ticket, shows no count of its children, and says why a start is refused", async () => {
    const ref = (key: string, state: "open" | "closed") => ({ ...card(key, "new"), state, state_reason: null });
    fakeApi(
      boardRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, {
          items: [
            card("1", "ready", { blocked_by: [ref("2", "open")], children: [ref("3", "closed"), ref("4", "open")] }),
            card("5", "ready", { blocked_by: [ref("2", "closed")] }),
          ],
          next_cursor: null,
        }),
        "PUT /api/v1/repos/acme/demo/tickets/1/board": apiError(409, "blocked", "Ticket 1 is blocked by open tickets."),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    const one = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(one).getByText("Blocked")).toBeInTheDocument();
    expect(within(one).queryByText(/Children/)).not.toBeInTheDocument();
    const five = screen.getByRole("article", { name: "acme/demo#5 Title of 5" });
    expect(within(five).queryByText("Blocked")).not.toBeInTheDocument();

    await pickFromMenu(user, "Actions of acme/demo#1", "In progress");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not move acme/demo#1; it is back where it was. It can be moved to In progress once every ticket it is blocked by is closed.",
    );
  });

  it("marks an interactive card, and only that one", async () => {
    fakeApi(
      boardRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [card("1", "ready", { interactive: true }), card("2", "ready")], next_cursor: null }),
      }),
    );
    renderApp("/acme/-/board");
    const one = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(one).getByText("Interactive")).toBeInTheDocument();
    const two = screen.getByRole("article", { name: "acme/demo#2 Title of 2" });
    expect(within(two).queryByText("Interactive")).not.toBeInTheDocument();
  });

  it("names the ancestor a card is interactive through, whatever its own switch", async () => {
    const ancestor = { ...card("1", "ready", { interactive: true }), state: "open" as const, state_reason: null };
    fakeApi(
      boardRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, {
          items: [card("2", "ready", { interactive_ancestor: ancestor }), card("3", "ready", { interactive: true, interactive_ancestor: ancestor })],
          next_cursor: null,
        }),
      }),
    );
    renderApp("/acme/-/board");
    for (const name of ["acme/demo#2 Title of 2", "acme/demo#3 Title of 3"]) {
      const inherited = await screen.findByRole("article", { name });
      expect(within(inherited).getByText("Interactive via acme/demo#1")).toBeInTheDocument();
      expect(within(inherited).queryByText("Interactive")).not.toBeInTheDocument();
    }
  });

  it("refetches the board after a concurrent change (409)", async () => {
    const fake = fakeApi(boardRoutes({ "PUT /api/v1/repos/acme/web/tickets/2/board": apiError(409, "conflict", "Changed.") }));
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 2");
    const before = fake.calls("GET", "/api/v1/workspaces/acme/board/cards").length;

    await pickFromMenu(user, "Actions of acme/web#2", "New");

    expect(await screen.findByText("acme/web#2 was changed by someone else meanwhile; the board was reloaded.")).toBeInTheDocument();
    await waitFor(() => expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards").length).toBeGreaterThan(before));
  });

  it("adds a ticket to the lane it is typed in, in the chosen repository, and remembers that repository", async () => {
    const fake = fakeApi(
      boardRoutes({
        "POST /api/v1/repos/acme/web/tickets": json(201, card("9", "ready", { repository: "acme/web" })),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await user.click(screen.getByRole("button", { name: "Add a ticket to Ready" }));
    const form = screen.getByRole("form", { name: "Add a ticket to Ready" });
    // Only the workspace's repositories are offered.
    await user.click(within(form).getByRole("combobox", { name: "Repository" }));
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["acme/demo", "acme/web"]);
    await user.click(screen.getByRole("option", { name: "acme/web" }));
    await user.type(within(form).getByRole("textbox", { name: "Title" }), "Broken button{Enter}");

    expect(await screen.findByText("acme/web#9 was created.")).toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")[0]?.body).toEqual({
      title: "Broken button",
      body: "",
      labels: [],
      lane: "ready",
    });
    expect(fake.requests.filter((r) => r.method === "PUT")).toHaveLength(0);
    // Another lane starts with the repository chosen last.
    await user.click(screen.getByRole("button", { name: "Add a ticket to Done" }));
    expect(within(screen.getByRole("form", { name: "Add a ticket to Done" })).getByRole("combobox", { name: "Repository" })).toHaveValue("acme/web");
  });

  it("attaches an image to a new ticket's body in the repository chosen in the modal, and creates it once stored", async () => {
    let answer!: () => void;
    const id = "atc_01JAAAAAAAAAAAAAAAAAAAAAAA";
    const url = `/api/v1/repos/acme%2Fweb/attachments/${id}`;
    const fake = fakeApi(
      boardRoutes({
        "POST /api/v1/repos/acme/web/attachments": () =>
          new Promise((resolve) => {
            answer = () => resolve(json(201, { id, name: "shot.png", media_type: "image/png", size: 1, url, created_at: "2026-10-14T10:00:00.000Z" }));
          }),
        "POST /api/v1/repos/acme/web/tickets": json(201, card("9", "new", { repository: "acme/web" })),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");
    await user.click(screen.getByRole("button", { name: "Add a ticket to New" }));
    await user.type(screen.getByRole("textbox", { name: "Title" }), "Broken button");
    await user.click(screen.getByRole("button", { name: "Add a body and labels…" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket (New)" });
    await choose(user, "Repository", "acme/web", dialog);
    const body = within(dialog).getByRole("textbox", { name: "Description" });
    await user.type(body, "See:");
    fireEvent.drop(body, { dataTransfer: { files: [new File(["x"], "shot.png", { type: "image/png" })], types: ["Files"] } });
    expect(within(dialog).getByRole("button", { name: "Create ticket" })).toBeDisabled();
    // The image is the chosen repository's: the choice stays while it uploads and once it is in the body.
    const repository = within(dialog).getByRole("combobox", { name: "Repository" });
    expect(repository).toBeDisabled();
    await waitFor(() => expect(answer).toBeTypeOf("function"));
    await act(async () => answer());
    expect(body).toHaveValue(`See:![shot.png](${url})`);
    expect(repository).toBeDisabled();
    expect(within(dialog).getByText("Fixed once an image is attached")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")[0]?.body).toMatchObject({ title: "Broken button", body: `See:![shot.png](${url})` });
  });

  it("adds a ticket with its body and labels from the lane's modal", async () => {
    const fake = fakeApi(
      boardRoutes({
        "POST /api/v1/repos/acme/web/tickets": json(201, card("9", "in_progress", { repository: "acme/web" })),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/-/board");
    await screen.findByText("Title of 1");

    await user.click(screen.getByRole("button", { name: "Add a ticket to In progress" }));
    await user.type(screen.getByRole("textbox", { name: "Title" }), "Broken button");
    await user.click(screen.getByRole("button", { name: "Add a body and labels…" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket (In progress)" });
    // The title typed so far comes along; the labels are the chosen repository's.
    expect(within(dialog).getByRole("textbox", { name: "Title" })).toHaveValue("Broken button");
    expect(within(dialog).getByRole("checkbox", { name: "bug" })).toBeInTheDocument();
    await choose(user, "Repository", "acme/web", dialog);
    expect(within(dialog).queryByRole("checkbox", { name: "bug" })).not.toBeInTheDocument();
    await user.type(within(dialog).getByRole("textbox", { name: "Description" }), "It does nothing.");
    await user.click(within(dialog).getByRole("checkbox", { name: "ui" }));
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));

    await waitFor(() => expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")).toHaveLength(1));
    expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")[0]?.body).toEqual({
      title: "Broken button",
      body: "It does nothing.",
      labels: ["ui"],
      lane: "in_progress",
    });
  });
});
