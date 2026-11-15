import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Card, TicketRef } from "../api/types";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { card } from "./fixtures";

const WORKSPACE = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };
const AIKO = { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] };
const KENJI = { ...AIKO, name: "kenji", display_name: "Kenji" };
const MEMBERS = { items: [{ name: "owner", display_name: "Owner", kind: "owner", accounts: [], reviewers: [] }, AIKO, KENJI], next_cursor: null };

const ref = (c: Card): TicketRef => ({ url: c.url, repository: c.repository, key: c.key, title: c.title, state: c.state, state_reason: null });
const aiko = { assignee: { name: "aiko", assigned_at: "2026-11-01T00:00:00Z" } };

/**
 * Aiko's ready tickets, in board order: #1, #2 (in the closed parent #7), #3 (waits for #4), #4,
 * #6 (waits for the new #9, so it will not start); #5 is ready without an assignee, #8 is Kenji's.
 */
const P = card("7", "in_progress", aiko);
const N = card("9", "new");
const C4 = card("4", "ready", aiko);
const C2 = card("2", "ready", { ...aiko, parent: ref(P) });
const C6 = card("6", "ready", { ...aiko, blocked_by: [ref(N)] });
const CARDS: Card[] = [
  { ...N, blocks: [ref(C6)] },
  card("1", "ready", aiko),
  C2,
  card("3", "ready", { ...aiko, blocked_by: [ref(C4)] }),
  C4,
  card("5", "ready"),
  C6,
  card("8", "ready", { assignee: { name: "kenji", assigned_at: "2026-11-01T00:00:00Z" } }),
  { ...P, children: [ref(C2)] },
];

type Routes = Parameters<typeof fakeApi>[0];

function routes(extra: Routes = {}, session = SESSION): Routes {
  return {
    "GET /api/v1/session": json(200, session),
    "GET /api/v1/workspaces": json(200, { items: [WORKSPACE], next_cursor: null }),
    "GET /api/v1/workspaces/acme": json(200, WORKSPACE),
    "GET /api/v1/members": json(200, MEMBERS),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/workspaces/acme/board/cards": json(200, { items: CARDS, next_cursor: null }),
    ...extra,
  };
}

const article = (key: string) => screen.getByRole("article", { name: new RegExp(`^acme/demo#${key} `) });
const tagOf = (key: string) => within(article(key)).queryByRole("button", { name: /click to open the pickup order$/ });
const panel = () => screen.queryByRole("complementary", { name: "Pickup order" });
const rowOf = (key: string) => within(screen.getByRole("list", { name: "Pickup order of aiko" })).getByText(`acme/demo#${key}`).closest("li") as HTMLElement;
const rowKeys = () => within(screen.getByRole("list", { name: "Pickup order of aiko" })).getAllByRole("listitem").map((li) => li.querySelector(".key")?.textContent);
const dataTransfer = () => ({ setData: () => {}, getData: () => "", effectAllowed: "" });
/** A drag event at a height (jsdom's events built by fireEvent.dragOver carry no coordinates). */
const dragAt = (type: "dragover" | "drop", el: HTMLElement, clientY: number) => fireEvent(el, new MouseEvent(type, { bubbles: true, cancelable: true, clientY }));
/** Drags a row onto the top half of another (rows are 40px high). */
function dragRow(from: string, onto: string) {
  for (const key of [from, onto]) rowOf(key).getBoundingClientRect = () => new DOMRect(0, 0, 300, 40);
  fireEvent.dragStart(rowOf(from), { dataTransfer: dataTransfer() });
  dragAt("dragover", rowOf(onto), 5);
  dragAt("drop", rowOf(onto), 5);
}

async function shown(extra?: Routes) {
  const fake = fakeApi(routes(extra));
  renderApp("/acme");
  await waitFor(() => expect(tagOf("1")).toBeInTheDocument());
  return fake;
}

async function opened(user: ReturnType<typeof userEvent.setup>, key = "1", extra?: Routes) {
  const fake = await shown(extra);
  await user.click(tagOf(key) as HTMLElement);
  await screen.findByRole("complementary", { name: "Pickup order" });
  return fake;
}

describe("pickup order", () => {
  it("numbers each agent's ready cards in the order the owner set, marking one that will not start", async () => {
    await shown();
    expect(tagOf("1")).toHaveTextContent(/^1$/);
    expect(tagOf("1")).toHaveAccessibleName("Number 1 in aiko's pickup order — click to open the pickup order");
    // Joined to the card's lane: the number is its place among that lane's tickets.
    const lane = within(article("1")).getByRole("button", { name: /^Lane of acme\/demo#1/ });
    expect(lane.nextElementSibling).toBe(tagOf("1"));
    expect(lane.parentElement).toHaveClass("lane-pickup");
    // Waiting for #4, still open, #3 keeps its place: when it starts is the system's.
    expect(tagOf("3")).toHaveTextContent(/^3$/);
    expect(tagOf("4")).toHaveTextContent(/^4$/);
    expect(tagOf("6")).toHaveTextContent(/^5!$/);
    expect(tagOf("6")).toHaveClass("stuck");
    expect(tagOf("6")).toHaveAccessibleName(
      "Number 5 in aiko's pickup order. Will not start as things are: acme/demo#9 is in New — click to open the pickup order",
    );
    expect(tagOf("8")).toHaveTextContent(/^1$/);
    // Neither unassigned, new, nor in progress tickets are numbered.
    expect(tagOf("5")).not.toBeInTheDocument();
    expect(tagOf("9")).not.toBeInTheDocument();
    expect(tagOf("7")).not.toBeInTheDocument();
  });

  it("shows no tags outside the owner's session", async () => {
    fakeApi(routes({}, { ...SESSION, me: AIKO }));
    renderApp("/acme");
    await screen.findByRole("article", { name: /^acme\/demo#1 / });
    expect(document.querySelector(".pickup-tag")).toBeNull();
  });

  it("opens beside the figure at the clicked card's row, and closes by the tag, Escape, or its button", async () => {
    const user = userEvent.setup();
    await shown();
    // The tag does not start the card's drag.
    expect(fireEvent.mouseDown(tagOf("4") as HTMLElement)).toBe(false);
    await user.click(tagOf("4") as HTMLElement);
    const aside = await screen.findByRole("complementary", { name: "Pickup order" });
    expect(tagOf("4")).toHaveAttribute("aria-expanded", "true");
    expect(rowOf("4")).toHaveAttribute("aria-current", "true");
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#2", "acme/demo#3", "acme/demo#4", "acme/demo#6"]);
    expect(within(aside).getByRole("combobox", { name: "Assignee" })).toHaveValue("aiko");
    // A row: its number, why it will not start (if so), the ticket, and its parent; nothing of work in progress.
    expect(rowOf("3")).toHaveTextContent(/^3acme\/demo#3 Title of 3$/);
    expect(rowOf("6")).toHaveTextContent("5Won't start: acme/demo#9 is in New");
    expect(rowOf("2")).toHaveTextContent("Parent: acme/demo#7");
    expect(aside).not.toHaveTextContent(/in progress|pull request|waiting|reply/i);
    // The figure stays usable beside it.
    expect(within(article("1")).getByRole("button", { name: /^Lane of acme\/demo#1/ })).toBeEnabled();

    await user.click(tagOf("4") as HTMLElement);
    expect(panel()).not.toBeInTheDocument();
    await user.click(tagOf("4") as HTMLElement);
    await user.keyboard("{Escape}");
    expect(panel()).not.toBeInTheDocument();
    await user.click(tagOf("4") as HTMLElement);
    await user.click(screen.getByRole("button", { name: "Close the pickup order" }));
    expect(panel()).not.toBeInTheDocument();
  });

  it("switches to another agent's order", async () => {
    const user = userEvent.setup();
    await opened(user);
    const select = within(panel() as HTMLElement).getByRole("combobox", { name: "Assignee" });
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["aiko", "kenji"]);
    await user.selectOptions(select, "kenji");
    const rows = within(screen.getByRole("list", { name: "Pickup order of kenji" })).getAllByRole("listitem");
    expect(rows.map((li) => li.textContent)).toEqual(["1acme/demo#8 Title of 8"]);
  });

  it("marks a row's card in the figure, or the closed parent it is in, and says when the filters hide it", async () => {
    const user = userEvent.setup();
    await opened(user);
    await user.hover(rowOf("4"));
    expect(article("4")).toHaveClass("highlighted");
    await user.unhover(rowOf("4"));
    expect(article("4")).not.toHaveClass("highlighted");
    // #2 is inside the closed #7: #7 is marked, and stays closed.
    fireEvent.focus(rowOf("2"));
    expect(article("7")).toHaveClass("highlighted");
    expect(screen.queryByRole("article", { name: /^acme\/demo#2 / })).not.toBeInTheDocument();
    fireEvent.blur(rowOf("2"));

    await user.type(screen.getByRole("textbox", { name: "Text" }), "Title of 4");
    await waitFor(() => expect(screen.queryByRole("article", { name: /^acme\/demo#1 / })).not.toBeInTheDocument());
    expect(rowOf("1")).toHaveTextContent("Hidden by the filters");
    expect(rowOf("4")).not.toHaveTextContent("Hidden by the filters");
    // Numbered before the filters: #4 is still fourth.
    expect(tagOf("4")).toHaveTextContent(/^4$/);
  });

  it("puts a dragged row before another, bringing what it waits for along, and undoes it", async () => {
    const user = userEvent.setup();
    const fake = await opened(user, "1", {
      "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, {}),
      "PUT /api/v1/repos/acme/demo/tickets/3/board": json(200, {}),
    });
    dragRow("3", "1");
    await screen.findByText("Changed the pickup order: acme/demo#3, with acme/demo#4 it waits for moved before it.");
    const puts = () => fake.requests.filter((r) => r.method === "PUT").map((r) => [r.path.split("/")[7], r.body]);
    expect(puts()).toEqual([
      ["4", { lane: "ready", before: "acme/demo#1" }],
      ["3", { lane: "ready", before: "acme/demo#1" }],
    ]);

    await user.click(screen.getByRole("button", { name: "Undo" }));
    await screen.findByText("Undone.");
    // Bottom up: #4 back before #5 (what followed it), then #3 before #4.
    expect(puts().slice(2)).toEqual([
      ["4", { lane: "ready", before: "acme/demo#5" }],
      ["3", { lane: "ready", before: "acme/demo#4" }],
    ]);
  });

  it("undoes against the board as it is when Undo is clicked", async () => {
    const user = userEvent.setup();
    // Once written, #5 (what followed #4) and #3 (moved) are started before Undo is clicked.
    let written = false;
    const started = CARDS.map((c) => (c.key === "5" || c.key === "3" ? { ...c, lane: "in_progress" as const } : c));
    const put = () => {
      written = true;
      return json(200, {});
    };
    const fake = await opened(user, "1", {
      "PUT /api/v1/repos/acme/demo/tickets/4/board": put,
      "PUT /api/v1/repos/acme/demo/tickets/3/board": put,
      "GET /api/v1/workspaces/acme/board/cards": (req) => {
        const board = written ? started : CARDS;
        return json(200, { items: req.search.includes("lane=ready") ? board.filter((c) => c.lane === "ready").map((c) => ({ ...c, comments: [] })) : board, next_cursor: null });
      },
    });
    dragRow("3", "1");
    await screen.findByText("Changed the pickup order: acme/demo#3, with acme/demo#4 it waits for moved before it.");
    await waitFor(() => expect(tagOf("3")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Undo" }));
    await screen.findByText("Undone.");
    // #4 goes before #6, the next still in Ready; #3, started, stays in progress.
    expect(fake.requests.filter((r) => r.method === "PUT").slice(2).map((r) => [r.path.split("/")[7], r.body])).toEqual([["4", { lane: "ready", before: "acme/demo#6" }]]);
  });

  it("says there is nothing to put back when every moved ticket has left Ready", async () => {
    const user = userEvent.setup();
    let written = false;
    const started = CARDS.map((c) => (c.key === "1" ? { ...c, lane: "in_progress" as const } : c));
    const fake = await opened(user, "1", {
      "PUT /api/v1/repos/acme/demo/tickets/1/board": () => {
        written = true;
        return json(200, {});
      },
      "GET /api/v1/workspaces/acme/board/cards": (req) => {
        const board = written ? started : CARDS;
        return json(200, { items: req.search.includes("lane=ready") ? board.filter((c) => c.lane === "ready").map((c) => ({ ...c, comments: [] })) : board, next_cursor: null });
      },
    });
    for (const key of ["1", "6"]) rowOf(key).getBoundingClientRect = () => new DOMRect(0, 0, 300, 40);
    fireEvent.dragStart(rowOf("1"), { dataTransfer: dataTransfer() });
    dragAt("dragover", rowOf("6"), 35);
    dragAt("drop", rowOf("6"), 35);
    await screen.findByText("Changed the pickup order: acme/demo#1.");
    await waitFor(() => expect(tagOf("1")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("The moved tickets have all left the Ready lane meanwhile: there is nothing to put back.")).toBeInTheDocument();
    expect(fake.requests.filter((r) => r.method === "PUT")).toHaveLength(1);
  });

  it("puts a row dropped below the last after the last row only", async () => {
    const user = userEvent.setup();
    const fake = await opened(user, "1", { "PUT /api/v1/repos/acme/demo/tickets/1/board": json(200, {}) });
    for (const key of ["1", "6"]) rowOf(key).getBoundingClientRect = () => new DOMRect(0, 0, 300, 40);
    fireEvent.dragStart(rowOf("1"), { dataTransfer: dataTransfer() });
    dragAt("dragover", rowOf("6"), 35);
    // Shown before it is written: the numbers follow.
    expect(rowKeys()).toEqual(["acme/demo#2", "acme/demo#3", "acme/demo#4", "acme/demo#6", "acme/demo#1"]);
    expect(tagOf("4")).toHaveTextContent(/^3$/);
    expect(tagOf("1")).toHaveTextContent(/^5$/);
    dragAt("drop", rowOf("6"), 35);
    await screen.findByText("Changed the pickup order: acme/demo#1.");
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/1/board").map((r) => r.body)).toEqual([{ lane: "ready", after: "acme/demo#6" }]);
  });

  it("stops at a refusal, says it, and leaves what was written undoable", async () => {
    const user = userEvent.setup();
    const fake = await opened(user, "1", {
      "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, {}),
      "PUT /api/v1/repos/acme/demo/tickets/3/board": apiError(422, "invalid", "no"),
    });
    dragRow("3", "1");
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not move acme/demo#3");
    expect(fake.requests.filter((r) => r.method === "PUT")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await screen.findByText("Undone.");
    expect(fake.requests.filter((r) => r.method === "PUT").at(-1)?.body).toEqual({ lane: "ready", before: "acme/demo#5" });
  });

  it("ends a move shown before it is written when the panel closes", async () => {
    const user = userEvent.setup();
    await opened(user);
    rowOf("4").focus();
    await user.keyboard(" {ArrowUp}{ArrowUp}{ArrowUp}");
    expect(tagOf("4")).toHaveTextContent(/^1$/);
    expect(tagOf("1")).toHaveTextContent(/^2$/);
    await user.click(screen.getByRole("button", { name: "Close the pickup order" }));
    expect(tagOf("1")).toHaveTextContent(/^1$/);
    expect(tagOf("4")).toHaveTextContent(/^4$/);
    // Reopened, it starts from the order as it is.
    await user.click(tagOf("1") as HTMLElement);
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#2", "acme/demo#3", "acme/demo#4", "acme/demo#6"]);
  });

  it("moves nothing until a row is moved, and keeps a drag still over a row it pulls along or between rows", async () => {
    const user = userEvent.setup();
    const fake = await opened(user);
    // #3 waits for #4, below it: lifted and put down, nothing moved or written.
    rowOf("3").focus();
    await user.keyboard(" ");
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#2", "acme/demo#3", "acme/demo#4", "acme/demo#6"]);
    await user.keyboard(" ");
    expect(fake.requests.filter((r) => r.method === "PUT")).toEqual([]);
    expect(screen.queryByText(/Changed the pickup order/)).not.toBeInTheDocument();

    for (const key of ["1", "3", "4"]) rowOf(key).getBoundingClientRect = () => new DOMRect(0, 0, 300, 40);
    fireEvent.dragStart(rowOf("3"), { dataTransfer: dataTransfer() });
    dragAt("dragover", rowOf("1"), 5);
    const pulled = ["acme/demo#4", "acme/demo#3", "acme/demo#1", "acme/demo#2", "acme/demo#6"];
    expect(rowKeys()).toEqual(pulled);
    // #4, pulled along, now stands where #1 did: over it, the drag stays.
    dragAt("dragover", rowOf("4"), 5);
    expect(rowKeys()).toEqual(pulled);
    // Between rows (the list itself, above its last row's bottom), too.
    const list = screen.getByRole("list", { name: "Pickup order of aiko" });
    (list.lastElementChild as HTMLElement).getBoundingClientRect = () => new DOMRect(0, 160, 300, 40);
    dragAt("dragover", list, 100);
    expect(rowKeys()).toEqual(pulled);
    dragAt("dragover", list, 230);
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#2", "acme/demo#4", "acme/demo#6", "acme/demo#3"]);
    fireEvent.dragEnd(rowOf("3"));
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#2", "acme/demo#3", "acme/demo#4", "acme/demo#6"]);
  });

  it("stays open on an Escape that belongs to a field or a drawer", async () => {
    const user = userEvent.setup();
    await opened(user, "1", {
      "GET /api/v1/repos/acme/demo": json(200, { path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: [], post_merge_jobs: [], required_checks: [], labels: [], upstream: null, revision: 1 }),
      "GET /api/v1/repos/acme/demo/tickets/1": json(200, CARDS[1]),
      "GET /api/v1/repos/acme/demo/tickets/1/comments": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/repos/acme/demo/tickets/1/pulls": json(200, { items: [], next_cursor: null }),
    });
    await user.type(screen.getByRole("textbox", { name: "Text" }), "x{Escape}");
    expect(panel()).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Text" }));
    await user.click(within(article("1")).getByRole("link", { name: "Title of 1" }));
    await screen.findByRole("dialog", { name: "Ticket" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Ticket" })).not.toBeInTheDocument());
    expect(panel()).toBeInTheDocument();
  });

  it("says when the members cannot be read, and tags nothing", async () => {
    fakeApi(routes({ "GET /api/v1/members": apiError(500, "internal", "members down") }));
    renderApp("/acme");
    expect(await screen.findByText(/members down/)).toBeInTheDocument();
    expect(document.querySelector(".pickup-tag")).toBeNull();
  });

  it("is reordered with the keyboard, and says so", async () => {
    const user = userEvent.setup();
    const fake = await opened(user, "1", { "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, {}) });
    rowOf("4").focus();
    await user.keyboard(" ");
    expect(screen.getByText(/^acme\/demo#4 lifted, number 4 of 5\./)).toBeInTheDocument();
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(screen.getByText("acme/demo#4: number 2 of 5.")).toBeInTheDocument();
    expect(rowKeys()).toEqual(["acme/demo#1", "acme/demo#4", "acme/demo#2", "acme/demo#3", "acme/demo#6"]);
    // Escape cancels the move, not the panel.
    await user.keyboard("{Escape}");
    expect(screen.getByText("Moving acme/demo#4 was cancelled.")).toBeInTheDocument();
    expect(panel()).toBeInTheDocument();
    expect(rowKeys()[3]).toBe("acme/demo#4");

    rowOf("4").focus();
    await user.keyboard(" {ArrowUp} ");
    expect(screen.getByText("acme/demo#4 dropped at number 3 of 5.")).toBeInTheDocument();
    await screen.findByText("Changed the pickup order: acme/demo#4.");
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/board").map((r) => r.body)).toEqual([{ lane: "ready", before: "acme/demo#3" }]);
  });
});
