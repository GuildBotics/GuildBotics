import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Card, PostMergeStatus } from "../api/types";
import { applyChange } from "../query/events";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose, pickFromMenu } from "../test/mantine";
import { ciUrlsOf } from "../views/board/CardView";
import { card } from "./fixtures";

const WORKSPACE = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };

const repo = (path: string, labels: string[]) => ({
  path,
  default_branch: "main",
  clone_url: "",
  url: "",
  protected_branches: ["main"],
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

function flowRoutes(extra: Record<string, Parameters<typeof fakeApi>[0][string]> = {}) {
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
              card("1", "ready", {
                labels: ["bug"],
                assignee: { name: "aiko", assigned_at: "2026-10-14T10:00:00.000Z" },
                pull_requests: [
                  {
                    url: `${origin}/workspaces/acme/repos/demo/pulls/3`,
                    label: "!3",
                    title: "Fix",
                    state: "open",
                    merged: false,
                    updated_at: "",
                  },
                ],
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
  checks: [
    { name: "test", status: "completed", conclusion: "failure", attempt_id: "att_1", attempt: 1 },
  ],
  checks_expected: true,
  base_checks: 1,
  base_failure: null,
  required_names: [],
};

it.each([true, false])("shows repair badge only on repair cards (%s)", async (repair) => {
  fakeApi(
    flowRoutes({
      "GET /api/v1/workspaces/acme/board/cards": json(200, {
        items: [card("1", "ready", { repair })],
        next_cursor: null,
      }),
    }),
  );
  renderApp("/workspaces/acme/task-flow", "ja");
  await screen.findByText("Title of 1");
  expect(screen.queryByText("修復") !== null).toBe(repair);
});

describe("task flow cards", () => {
  it("loads open PR checks before spending the remaining budget on merged PRs", () => {
    const pull = (number: number, merged: boolean) => ({
      url: `${window.location.origin}/workspaces/acme/repos/demo/pulls/${number}`,
      label: `!${number}`,
      title: "Change",
      state: merged ? ("closed" as const) : ("open" as const),
      merged,
      updated_at: "",
    });
    const merged = Array.from({ length: 31 }, (_, index) => pull(index + 1, true));
    const open = pull(32, false);
    const closed = { ...pull(33, false), state: "closed" as const };
    const urls = ciUrlsOf([
      card("1", "done", { pull_requests: [closed, ...merged] }),
      card("2", "in_progress", { pull_requests: [open] }),
      card("3", "ready", { pull_requests: [open] }),
    ]);
    expect([...urls]).toEqual([open.url, ...merged.slice(0, 29).map((p) => p.url)]);
  });

  it("replaces cached head checks with live post-merge verification", async () => {
    let merged = false;
    let status: PostMergeStatus | null = "pending";
    fakeApi(
      flowRoutes({
        "GET /api/v1/workspaces/acme/board/cards": () =>
          json(200, {
            items: [
              card("1", "done", {
                pull_requests: [
                  {
                    url: `${window.location.origin}/workspaces/acme/repos/demo/pulls/3`,
                    label: "!3",
                    title: "Fix",
                    state: merged ? "closed" : "open",
                    merged,
                    updated_at: "",
                  },
                ],
              }),
            ],
            next_cursor: null,
          }),
        "GET /api/v1/repos/acme/demo/pulls/3/checks": () =>
          json(
            200,
            merged
              ? {
                  ...CHECKS,
                  checks: [],
                  post_merge_checks: { status, sha: "merge-commit" },
                }
              : CHECKS,
          ),
      }),
    );
    const { client } = renderApp("/workspaces/acme/task-flow");
    expect(await screen.findByText("CI: failing")).toBeInTheDocument();
    merged = true;
    act(() => applyChange({ kind: "pull", path: "acme/demo", id: "3" }, client));
    expect(
      await screen.findByRole("link", { name: "Post-merge verification: pending" }),
    ).toHaveAttribute("href", "/workspaces/acme/repos/demo/pulls/3#post-merge-checks");
    expect(screen.queryByText("CI: failing")).not.toBeInTheDocument();
    const checkChanged = () =>
      act(() => applyChange({ kind: "check", path: "acme/demo", id: "merge-commit" }, client));
    status = "failing";
    checkChanged();
    expect(await screen.findByText("Post-merge verification: failing")).toBeInTheDocument();
    status = "passing";
    checkChanged();
    expect(await screen.findByText("Post-merge verification: passing")).toBeInTheDocument();
    expect(screen.queryByText("Post-merge verification: pending")).not.toBeInTheDocument();
    status = null;
    checkChanged();
    await waitFor(() =>
      expect(screen.queryByText(/Post-merge verification:/)).not.toBeInTheDocument(),
    );
  });

  it("renders cards from every page with their lane and PR checks", async () => {
    const fake = fakeApi(flowRoutes());
    renderApp("/workspaces/acme/task-flow");

    await screen.findByRole("article", { name: "acme/web#2 Title of 2" });
    expect(
      await screen.findByRole("article", { name: "acme/demo#3 Title of 3" }),
    ).toHaveTextContent("In progress");
    expect(screen.getByRole("article", { name: "acme/demo#1 Title of 1" })).toHaveTextContent(
      "Ready",
    );
    expect(await screen.findByText(/CI: failing/)).toBeInTheDocument();
    const [first, second] = fake.calls("GET", "/api/v1/workspaces/acme/board/cards");
    expect(first?.search).toContain("state=open");
    expect(first?.search).toContain("comments=none");
    expect(second?.search).toContain("cursor=page2");
  });

  it("opens a ticket in a drawer at the task flow URL, with the ticket's URL to copy; a ticket linked there replaces it", async () => {
    const ticket = (key: string, extra: Partial<Card> = {}) => ({
      [`GET /api/v1/repos/acme/demo/tickets/${key}`]: json(200, card(key, "new", extra)),
      [`GET /api/v1/repos/acme/demo/tickets/${key}/comments`]: json(200, {
        items: [
          {
            id: `cmt_${key}`,
            author: "comment-agent",
            body: "Drawer comment",
            meta: null,
            created_at: "2026-10-14T10:00:00.000Z",
            reactions: [],
            url: "",
          },
        ],
        next_cursor: null,
      }),
      [`GET /api/v1/repos/acme/demo/tickets/${key}/pulls`]: json(200, {
        items: [],
        next_cursor: null,
      }),
    });
    const three = card("3", "in_progress");
    const blocker = {
      url: three.url,
      repository: "acme/demo",
      key: "3",
      title: three.title,
      state: "open" as const,
      state_reason: null,
    };
    fakeApi(
      flowRoutes({
        "GET /api/v1/repos/acme/demo": json(200, repo("acme/demo", ["bug"])),
        ...ticket("1", { blocked_by: [blocker] }),
        ...ticket("3"),
      }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");

    // A click with a modifier opens the ticket's page as a link does, not the drawer.
    fireEvent.click(await screen.findByRole("link", { name: "Title of 1" }), { ctrlKey: true });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Title of 1" }));
    const drawer = await screen.findByRole("dialog", { name: "Ticket" });
    expect(await within(drawer).findByRole("heading", { level: 2 })).toHaveTextContent(
      "#1 Title of 1",
    );
    expect(window.location.pathname).toBe("/workspaces/acme/task-flow");
    await user.click(within(drawer).getByRole("button", { name: "Copy the ticket's URL" }));
    expect(await navigator.clipboard.readText()).toBe(
      `${window.location.origin}/workspaces/acme/repos/demo/tickets/1`,
    );
    await user.click(await within(drawer).findByRole("button", { name: "Copy the comment's URL" }));
    expect(await navigator.clipboard.readText()).toBe(
      `${window.location.origin}/workspaces/acme/repos/demo/tickets/1#cmt_1`,
    );
    expect(within(drawer).getByTitle("Link to this comment")).toHaveAttribute(
      "href",
      "/workspaces/acme/repos/demo/tickets/1#cmt_1",
    );

    await user.click(within(drawer).getByRole("link", { name: /Title of 3/ }));
    expect(
      await within(drawer).findByRole("heading", { level: 2, name: /#3 Title of 3/ }),
    ).toBeInTheDocument();
    expect(window.location.pathname).toBe("/workspaces/acme/task-flow");
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
    await user.click(
      await within(await screen.findByRole("dialog", { name: "Ticket" })).findByRole("button", {
        name: "Close the drawer",
      }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Task flow" })).toBeInTheDocument();
  });

  it("mounts a card's assignee menu only once asked for, and assigns from it", async () => {
    const fake = fakeApi(
      flowRoutes({ "PUT /api/v1/repos/acme/web/tickets/2/assignee": json(200, {}) }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    await screen.findByText("Title of 2");
    expect(screen.queryAllByRole("menu")).toHaveLength(0);

    // The assignee offers the owner and agents, not runners.
    await user.click(
      screen.getByRole("button", { name: "Assignee of acme/web#2: Unassigned — click to change" }),
    );
    expect(await screen.findByRole("menuitem", { name: "owner" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "ci" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "aiko" }));

    await waitFor(() =>
      expect(fake.calls("PUT", "/api/v1/repos/acme/web/tickets/2/assignee")).toHaveLength(1),
    );
    expect(fake.calls("PUT", "/api/v1/repos/acme/web/tickets/2/assignee")[0]?.body).toEqual({
      name: "aiko",
    });
  });

  it("shows a card's one assignee and clears it", async () => {
    const fake = fakeApi(
      flowRoutes({ "DELETE /api/v1/repos/acme/demo/tickets/1/assignee": json(200, {}) }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    const card1 = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(card1).getAllByText("aiko", { selector: ".assignee" })).toHaveLength(1);

    await pickFromMenu(user, "Assignee of acme/demo#1: aiko — click to change", "No one");

    await waitFor(() =>
      expect(fake.calls("DELETE", "/api/v1/repos/acme/demo/tickets/1/assignee")).toHaveLength(1),
    );
  });

  it("leaves the copy item unchanged when the clipboard refuses", async () => {
    fakeApi(flowRoutes());
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
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

  it("asks for closed tickets too when the user includes them, and remembers that on this browser", async () => {
    const fake = fakeApi(flowRoutes());
    const user = userEvent.setup();
    const { unmount } = renderApp("/workspaces/acme/task-flow");
    await screen.findByText("Title of 1");
    expect(screen.getByLabelText("Include closed tickets")).not.toBeChecked();
    await user.click(screen.getByLabelText("Include closed tickets"));
    await waitFor(() =>
      expect(
        fake
          .calls("GET", "/api/v1/workspaces/acme/board/cards")
          .some((r) => r.search.includes("state=all")),
      ).toBe(true),
    );

    // Shown again (a reload, another workspace), the board keeps the choice.
    unmount();
    const again = renderApp("/workspaces/acme/task-flow");
    expect(await screen.findByLabelText("Include closed tickets")).toBeChecked();
    await user.click(screen.getByLabelText("Include closed tickets"));
    again.unmount();
    renderApp("/workspaces/acme/task-flow");
    expect(await screen.findByLabelText("Include closed tickets")).not.toBeChecked();
  });

  it("marks a card blocked by an open ticket, shows no count of its children, and says why a start is refused", async () => {
    const ref = (key: string, state: "open" | "closed") => ({
      ...card(key, "new"),
      state,
      state_reason: null,
    });
    fakeApi(
      flowRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, {
          items: [
            card("1", "ready", {
              blocked_by: [ref("2", "open")],
              children: [ref("3", "closed"), ref("4", "open")],
            }),
            card("5", "ready", { blocked_by: [ref("2", "closed")] }),
          ],
          next_cursor: null,
        }),
        "PUT /api/v1/repos/acme/demo/tickets/1/board": apiError(
          409,
          "blocked",
          "Ticket 1 is blocked by open tickets.",
        ),
      }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    const one = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(one).getByText("Blocked")).toBeInTheDocument();
    expect(within(one).queryByText(/Children/)).not.toBeInTheDocument();
    const five = screen.getByRole("article", { name: "acme/demo#5 Title of 5" });
    expect(within(five).queryByText("Blocked")).not.toBeInTheDocument();

    await pickFromMenu(user, "Lane of acme/demo#1: Ready — click to change", "In progress");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not move acme/demo#1; it is back where it was. It can be moved to In progress once every ticket it is blocked by is closed.",
    );
  });

  it("marks an interactive card, and only that one", async () => {
    fakeApi(
      flowRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, {
          items: [card("1", "ready", { interactive: true }), card("2", "ready")],
          next_cursor: null,
        }),
      }),
    );
    renderApp("/workspaces/acme/task-flow");
    const one = await screen.findByRole("article", { name: "acme/demo#1 Title of 1" });
    expect(within(one).getByText("Interactive")).toBeInTheDocument();
    const two = screen.getByRole("article", { name: "acme/demo#2 Title of 2" });
    expect(within(two).queryByText("Interactive")).not.toBeInTheDocument();
  });

  it("names the ancestor a card is interactive through, whatever its own switch", async () => {
    const ancestor = {
      ...card("1", "ready", { interactive: true }),
      state: "open" as const,
      state_reason: null,
    };
    fakeApi(
      flowRoutes({
        "GET /api/v1/workspaces/acme/board/cards": json(200, {
          items: [
            card("2", "ready", { interactive_ancestor: ancestor }),
            card("3", "ready", { interactive: true, interactive_ancestor: ancestor }),
          ],
          next_cursor: null,
        }),
      }),
    );
    renderApp("/workspaces/acme/task-flow");
    for (const name of ["acme/demo#2 Title of 2", "acme/demo#3 Title of 3"]) {
      const inherited = await screen.findByRole("article", { name });
      expect(within(inherited).getByText("Interactive via acme/demo#1")).toBeInTheDocument();
      expect(within(inherited).queryByText("Interactive")).not.toBeInTheDocument();
    }
  });

  it("refetches the board after a concurrent change (409)", async () => {
    const fake = fakeApi(
      flowRoutes({
        "PUT /api/v1/repos/acme/web/tickets/2/board": apiError(409, "conflict", "Changed."),
      }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    await screen.findByText("Title of 2");
    const before = fake.calls("GET", "/api/v1/workspaces/acme/board/cards").length;

    await pickFromMenu(user, "Lane of acme/web#2: Ready — click to change", "New");

    expect(
      await screen.findByText(
        "acme/web#2 was changed by someone else meanwhile; the board was reloaded.",
      ),
    ).toBeInTheDocument();
    await waitFor(() =>
      expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards").length).toBeGreaterThan(
        before,
      ),
    );
  });

  it("attaches an image to a new ticket's body in the repository chosen in the modal, and creates it once stored", async () => {
    let answer!: () => void;
    const id = "atc_01JAAAAAAAAAAAAAAAAAAAAAAA";
    const url = `/api/v1/repos/acme%2Fweb/attachments/${id}`;
    const fake = fakeApi(
      flowRoutes({
        "POST /api/v1/repos/acme/web/attachments": () =>
          new Promise((resolve) => {
            answer = () =>
              resolve(
                json(201, {
                  id,
                  name: "shot.png",
                  media_type: "image/png",
                  size: 1,
                  url,
                  created_at: "2026-10-14T10:00:00.000Z",
                }),
              );
          }),
        "POST /api/v1/repos/acme/web/tickets": json(
          201,
          card("9", "new", { repository: "acme/web" }),
        ),
      }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    await screen.findByText("Title of 1");
    await user.click(screen.getByRole("button", { name: "Add ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket (New)" });
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Broken button");
    await choose(user, "Repository", "acme/web", dialog);
    const body = within(dialog).getByRole("textbox", { name: "Description" });
    await user.type(body, "See:");
    fireEvent.drop(body, {
      dataTransfer: {
        files: [new File(["x"], "shot.png", { type: "image/png" })],
        types: ["Files"],
      },
    });
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
    await waitFor(() =>
      expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")).toHaveLength(1),
    );
    expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")[0]?.body).toMatchObject({
      title: "Broken button",
      body: `See:![shot.png](${url})`,
    });
  });

  it("adds a ticket with its body and labels and remembers the repository", async () => {
    const fake = fakeApi(
      flowRoutes({
        "POST /api/v1/repos/acme/web/tickets": json(
          201,
          card("9", "in_progress", { repository: "acme/web" }),
        ),
      }),
    );
    const user = userEvent.setup();
    renderApp("/workspaces/acme/task-flow");
    await screen.findByText("Title of 1");

    await user.click(screen.getByRole("button", { name: "Add ticket" }));
    const dialog = await screen.findByRole("dialog", { name: "New ticket (New)" });
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Broken button");
    await choose(user, "Lane", "In progress", dialog);
    expect(within(dialog).getByRole("checkbox", { name: "bug" })).toBeInTheDocument();
    await choose(user, "Repository", "acme/web", dialog);
    expect(within(dialog).queryByRole("checkbox", { name: "bug" })).not.toBeInTheDocument();
    await user.type(
      within(dialog).getByRole("textbox", { name: "Description" }),
      "It does nothing.",
    );
    await user.click(within(dialog).getByRole("checkbox", { name: "ui" }));
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));

    await waitFor(() =>
      expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")).toHaveLength(1),
    );
    expect(fake.calls("POST", "/api/v1/repos/acme/web/tickets")[0]?.body).toEqual({
      title: "Broken button",
      body: "It does nothing.",
      labels: ["ui"],
      lane: "in_progress",
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Add ticket" }));
    const next = await screen.findByRole("dialog", { name: "New ticket (New)" });
    expect(within(next).getByRole("combobox", { name: "Repository" })).toHaveValue("acme/web");
  });
});
