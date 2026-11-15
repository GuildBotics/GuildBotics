import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Card, TicketRef } from "../api/types";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose, pickFromMenu } from "../test/mantine";
import { card } from "./fixtures";

const WORKSPACE = { name: "acme", lanes: ["new", "ready", "in_progress", "done"] };

const MEMBERS = {
  items: [
    { name: "owner", display_name: "Owner", kind: "owner", accounts: [], reviewers: [] },
    { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] },
  ],
  next_cursor: null,
};

const url = (key: string) => `${window.location.origin}/acme/demo/-/tickets/${key}`;
const ref = (key: string, state: TicketRef["state"] = "open"): TicketRef => ({
  url: url(key),
  repository: "acme/demo",
  key,
  title: `Title of ${key}`,
  state,
  state_reason: null,
});

/**
 * #1 is the parent of #2 and #3, #2 waits for #4, #5 has no relation.
 * Collapsed, #1 stands for #2: the line from #4 is dotted.
 */
const CARDS: Card[] = [
  card("1", "new", { children: [ref("2"), ref("3")] }),
  card("4", "new", { blocks: [ref("2")] }),
  card("2", "ready", { parent: ref("1"), blocked_by: [ref("4")] }),
  card("3", "ready", { parent: ref("1") }),
  card("5", "done"),
];

type Routes = Parameters<typeof fakeApi>[0];

function routes(cards: { open: Card[]; all?: Card[] } = { open: CARDS }, extra: Routes = {}): Routes {
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [WORKSPACE], next_cursor: null }),
    "GET /api/v1/workspaces/acme": json(200, WORKSPACE),
    "GET /api/v1/members": json(200, MEMBERS),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/workspaces/acme/board/cards": (req) =>
      json(200, { items: req.search.includes("state=all") ? (cards.all ?? cards.open) : cards.open, next_cursor: null }),
    ...extra,
  };
}

const article = (key: string) => screen.getByRole("article", { name: new RegExp(`^acme/demo#${key} `) });
const anchor = (key: string) => article(key).closest("[data-ticket]") as HTMLElement;
/** What is dragged: the card's own box (an open parent's frame holds its children too). */
const draggable = (key: string) => article(key).closest(".rel-card") as HTMLElement;
/** A line behind the cards (with its "×" when solid), and its copy in front of them. */
const edge = (from: string, to: string) => document.querySelector<SVGGElement>(`.rel-lines g[data-from="acme/demo#${from}"][data-to="acme/demo#${to}"]`);
const inFront = (from: string, to: string) => document.querySelector(`.rel-front g[data-from="acme/demo#${from}"][data-to="acme/demo#${to}"]`);
const dataTransfer = () => ({ setData: () => {}, getData: () => "", effectAllowed: "" });

async function shown() {
  renderApp("/acme");
  return screen.findByRole("article", { name: /^acme\/demo#1 / });
}

/** Opens #1, showing #2 and #3 in its frame. */
async function openParent(user: ReturnType<typeof userEvent.setup>) {
  await user.click(within(article("1")).getByRole("button", { expanded: false }));
  await screen.findByRole("article", { name: /^acme\/demo#2 / });
}

describe("relations", () => {
  it("is reached from the sidebar and lays out parents, their collapsed dependencies, and tickets without relations", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(routes());
    renderApp("/acme/-/board");
    await user.click(await screen.findByRole("link", { name: "Task flow" }));
    await screen.findByRole("article", { name: /^acme\/demo#1 / });
    expect(window.location.pathname).toBe("/acme");
    expect(screen.getByRole("link", { name: "Task flow" })).toHaveAttribute("aria-current", "page");

    // #2 and #3 are inside the collapsed #1; #4's dependency of #2 is drawn to #1, dotted.
    expect(screen.queryByRole("article", { name: /^acme\/demo#2 / })).not.toBeInTheDocument();
    expect(edge("4", "1")).toHaveClass("dashed");
    // A dotted line has no "×": which child's dependency it is, is not one.
    expect(within(edge("4", "1") as unknown as HTMLElement).queryByRole("button")).not.toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards").every((r) => r.search.includes("state=open") && r.search.includes("comments=none"))).toBe(true);

    // #5 has no relation but is not new: a card of its own in the figure. #1 and #4 are related, so not among the new tickets.
    expect(article("5")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /New tickets/ })).not.toBeInTheDocument();
  });

  it("opens a ticket in a drawer over the figure, at the figure's URL", async () => {
    fakeApi(
      routes(undefined, {
        "GET /api/v1/repos/acme/demo": json(200, { path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: [], required_checks: [], labels: [], upstream: null, revision: 1 }),
        "GET /api/v1/repos/acme/demo/tickets/5": json(200, CARDS[4]),
        "GET /api/v1/repos/acme/demo/tickets/5/comments": json(200, { items: [], next_cursor: null }),
        "GET /api/v1/repos/acme/demo/tickets/5/pulls": json(200, { items: [], next_cursor: null }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme");
    await user.click(await screen.findByRole("link", { name: "Title of 5" }));
    const drawer = await screen.findByRole("dialog", { name: "Ticket" });
    expect(await within(drawer).findByRole("heading", { level: 2 })).toHaveTextContent("#5 Title of 5");
    expect(window.location.pathname).toBe("/acme");
  });

  it("opens a parent into a frame with a toggle that says so, and remembers it", async () => {
    const user = userEvent.setup();
    fakeApi(routes());
    await shown();
    const toggle = within(article("1")).getByRole("button", { name: "Open child tickets" });
    expect(toggle).toHaveClass("card-control");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent(/^▶$/);
    // First in the card, before the key. A parent does not count its children on the card.
    expect(toggle.parentElement?.firstElementChild).toBe(toggle);
    expect(toggle.nextElementSibling).toHaveTextContent("acme/demo#1");
    expect(within(article("1")).queryByText(/Children/)).not.toBeInTheDocument();
    // A parent has the colour of its header; a ticket without children does not.
    expect(article("1")).toHaveClass("parent");
    expect(article("4")).not.toHaveClass("parent");
    await user.hover(toggle);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Open child tickets");

    await user.click(toggle);
    expect(await screen.findByRole("article", { name: /^acme\/demo#2 / })).toBeInTheDocument();
    // The same button, still focused: opening does not rebuild the card.
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveTextContent(/^▼$/);
    expect(toggle).toHaveAccessibleName("Close child tickets");
    expect(toggle).toHaveFocus();
    expect(article("1")).toHaveClass("parent");
    // The frame is #1's anchor; the line from #4 now reaches #2 itself, solid.
    expect(anchor("1")).toHaveClass("rel-frame");
    expect(anchor("2").parentElement?.closest("[data-ticket]")).toBe(anchor("1"));
    expect(edge("4", "2")).not.toHaveClass("dashed");
    expect(within(edge("4", "2") as unknown as HTMLElement).getByRole("button")).toBeInTheDocument();
    expect(edge("4", "1")).toBeNull();
    expect(JSON.parse(window.localStorage.getItem("branvil.relations.open.acme") ?? "[]")).toEqual(["acme/demo#1"]);
  });

  it("copies a ticket's URL from its menu", async () => {
    const user = userEvent.setup();
    fakeApi(routes());
    await shown();
    await user.click(within(article("4")).getByRole("button", { name: "Actions of acme/demo#4" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((i) => i.textContent)).toEqual([
      "Add parent ticket",
      "Add child ticket",
      "Add next ticket",
      "Interactive",
      "Copy URL",
      "Delete",
    ]);
    await user.click(within(menu).getByRole("menuitem", { name: "Copy URL" }));
    expect(await navigator.clipboard.readText()).toBe(url("4"));
    expect(await within(menu).findByRole("menuitem", { name: "Copied" })).toBeInTheDocument();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("menu")).not.toBeInTheDocument());
  });

  it("makes a dropped card a child and the new tickets it relates ready, and undoes it", async () => {
    const fake = fakeApi(
      routes(undefined, {
        "PATCH /api/v1/repos/acme/demo/tickets/4": json(200, CARDS[1]),
        "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, CARDS[1]),
        "PUT /api/v1/repos/acme/demo/tickets/1/board": json(200, CARDS[0]),
      }),
    );
    await shown();
    fireEvent.dragStart(draggable("4"), { dataTransfer: dataTransfer() });
    fireEvent.dragOver(article("1"));
    expect(anchor("1")).toHaveClass("drop-target");
    fireEvent.drop(article("1"));

    expect(await screen.findByText("Made acme/demo#4 a child of acme/demo#1.")).toBeInTheDocument();
    const writes = () => fake.requests.filter((r) => r.method !== "GET").map((r) => [r.method, r.path.replace("/api/v1/repos/acme/demo/tickets/", ""), r.body]);
    expect(writes()).toEqual([
      ["PATCH", "4", { parent: url("1") }],
      ["PUT", "4/board", { lane: "ready" }],
      ["PUT", "1/board", { lane: "ready" }],
    ]);
    await userEvent.setup().click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("Undone.")).toBeInTheDocument();
    expect(writes().slice(3)).toEqual([
      ["PATCH", "4", { parent: null }],
      ["PUT", "4/board", { lane: "new" }],
      ["PUT", "1/board", { lane: "new" }],
    ]);
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  });

  it("offers no drop on the dragged card itself or its current parent", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(routes());
    await shown();
    await openParent(user);
    fireEvent.dragStart(draggable("2"), { dataTransfer: dataTransfer() });
    fireEvent.dragOver(anchor("1"));
    expect(anchor("1")).not.toHaveClass("drop-target");
    fireEvent.drop(anchor("1"));
    fireEvent.dragStart(draggable("2"), { dataTransfer: dataTransfer() });
    fireEvent.drop(article("2"));
    // A write would be sent after a tick (TanStack runs onMutate first).
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fake.requests.some((r) => r.method === "PATCH")).toBe(false);
  });

  it("makes a related new ticket dropped on the background ready: only one without relations is new", async () => {
    const fake = fakeApi(routes(undefined, { "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, CARDS[1]) }));
    await shown();
    fireEvent.dragStart(draggable("4"), { dataTransfer: dataTransfer() });
    fireEvent.drop(document.querySelector(".relations-canvas") as HTMLElement);
    expect(await screen.findByText("acme/demo#4 moved to Ready.")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/board").map((r) => r.body)).toEqual([{ lane: "ready" }]);
  });

  it("makes a new child taken out of its parent onto the background ready, and undoes both", async () => {
    const user = userEvent.setup();
    const open = [card("1", "ready", { children: [ref("2")] }), card("2", "new", { parent: ref("1") })];
    const fake = fakeApi(
      routes({ open }, { "PATCH /api/v1/repos/acme/demo/tickets/2": json(200, open[1]), "PUT /api/v1/repos/acme/demo/tickets/2/board": json(200, open[1]) }),
    );
    await shown();
    await openParent(user);
    fireEvent.dragStart(draggable("2"), { dataTransfer: dataTransfer() });
    fireEvent.drop(document.querySelector(".relations-canvas") as HTMLElement);
    expect(await screen.findByText("Took acme/demo#2 out of acme/demo#1.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Undo" }));
    expect(await screen.findByText("Undone.")).toBeInTheDocument();
    const writes = fake.requests.filter((r) => r.method !== "GET").map((r) => [r.method, r.path.replace("/api/v1/repos/acme/demo/tickets/", ""), r.body]);
    expect(writes).toEqual([
      ["PATCH", "2", { parent: null }],
      ["PUT", "2/board", { lane: "ready" }],
      ["PATCH", "2", { parent: url("1") }],
      ["PUT", "2/board", { lane: "new" }],
    ]);
  });

  it("colours a parent as one while none of its children is shown, with nothing to open", async () => {
    // #7's only child #6 is closed and closed tickets are not shown.
    fakeApi(routes({ open: [card("7", "ready", { children: [ref("6", "closed")] }), card("5", "ready")] }));
    renderApp("/acme");
    await screen.findByText("Title of 7");
    expect(article("7")).toHaveClass("parent");
    expect(within(article("7")).queryByRole("button", { name: "Open child tickets" })).not.toBeInTheDocument();
    expect(article("5")).not.toHaveClass("parent");
  });

  it("counts relations to tickets it does not show: such a ticket is not in the frame of new tickets, nor can it be dropped there", async () => {
    const user = userEvent.setup();
    // #8 (new) waits for the closed #9 and #7 (ready) has the closed child #6, neither shown; #5 has no relation.
    const open = [card("7", "ready", { children: [ref("6", "closed")] }), card("8", "new", { blocked_by: [ref("9", "closed")] }), card("5", "new")];
    fakeApi(routes({ open }));
    renderApp("/acme");
    const frame = await screen.findByRole("group", { name: "New tickets (1)" });
    await user.click(within(frame).getByRole("button", { name: "Open child tickets" }));
    expect(within(frame).getAllByRole("article").map((a) => a.getAttribute("aria-label")?.split(" ")[0])).toEqual(["acme/demo#5"]);
    expect(frame).not.toContainElement(article("8"));
    fireEvent.dragStart(draggable("7"), { dataTransfer: dataTransfer() });
    fireEvent.dragOver(frame);
    expect(frame).not.toHaveClass("drop-target");
  });

  it("takes a child out of its parent when it is dropped on the background", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(routes(undefined, { "PATCH /api/v1/repos/acme/demo/tickets/2": json(200, CARDS[2]) }));
    await shown();
    await openParent(user);
    fireEvent.dragStart(draggable("2"), { dataTransfer: dataTransfer() });
    const background = document.querySelector(".relations-canvas") as HTMLElement;
    fireEvent.dragOver(background);
    expect(background).toHaveClass("drop-target");
    fireEvent.drop(background);
    expect(await screen.findByText("Took acme/demo#2 out of acme/demo#1.")).toBeInTheDocument();
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/2")[0]?.body).toEqual({ parent: null });
  });

  it("draws a dependency from a card's handle: the card it is dropped on waits for it, and the new ones are ready", async () => {
    const fake = fakeApi(
      routes(undefined, {
        "PATCH /api/v1/repos/acme/demo/tickets/1": json(200, CARDS[0]),
        "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, CARDS[1]),
        "PUT /api/v1/repos/acme/demo/tickets/1/board": json(200, CARDS[0]),
      }),
    );
    await shown();
    fireEvent.pointerDown(within(article("4")).getByTitle("Drag to another card: it waits for acme/demo#4"), { button: 0 });
    fireEvent.pointerMove(article("1"));
    expect(await screen.findByText("acme/demo#1 waits for acme/demo#4")).toBeInTheDocument();
    fireEvent.pointerUp(article("1"));

    expect(await screen.findByText("acme/demo#1 now waits for acme/demo#4.")).toBeInTheDocument();
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/1")[0]?.body).toEqual({ add_blocked_by: [url("4")] });
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/board")[0]?.body).toEqual({ lane: "ready" });
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/1/board")[0]?.body).toEqual({ lane: "ready" });
    expect(screen.queryByText("acme/demo#1 waits for acme/demo#4")).not.toBeInTheDocument();
  });

  it("makes an open parent's card the header of its frame, where its lines start", async () => {
    const user = userEvent.setup();
    // #1 has the child #2 and blocks #9.
    fakeApi(
      routes({
        open: [
          card("1", "new", { children: [ref("2")], blocks: [ref("9")] }),
          card("2", "new", { parent: ref("1") }),
          card("9", "ready", { blocked_by: [ref("1")] }),
        ],
      }),
    );
    // jsdom lays nothing out: the frame is 600×400, its header 600×80 at its top, #9 to the right.
    const box = (left: number, top: number, width: number, height: number) => DOMRect.fromRect({ x: left, y: top, width, height });
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const ref = this.closest("[data-ticket]")?.getAttribute("data-ticket");
      if (ref === "acme/demo#1") return this.matches(".rel-frame") ? box(0, 0, 600, 400) : box(0, 0, 600, 80);
      if (ref === "acme/demo#9") return box(700, 100, 260, 80);
      return box(0, 0, 0, 0);
    });
    await shown();
    await openParent(user);
    expect(article("1").closest(".rel-card")?.parentElement).toBe(anchor("1"));
    expect(anchor("1").firstElementChild).toBe(article("1").closest(".rel-card"));
    // From the header's right edge, at its middle; not from the middle of the frame.
    await waitFor(() => expect(edge("1", "9")?.querySelector("path")?.getAttribute("d")).toMatch(/^M 600 40 /));
  });

  it("pushes a ticket down level with the topmost ticket it waits for, never up", async () => {
    // #9 waits for #1 and #2; #8 waits for #1 and stands below it already.
    fakeApi(
      routes({
        open: [
          card("1", "ready", { blocks: [ref("9"), ref("8")] }),
          card("2", "ready", { blocks: [ref("9")] }),
          card("9", "ready", { blocked_by: [ref("1"), ref("2")] }),
          card("8", "ready", { blocked_by: [ref("1")] }),
        ],
      }),
    );
    // jsdom lays nothing out: each card where its column puts it, moved down by its margin.
    const natural: Record<string, [number, number, number]> = { "acme/demo#1": [0, 300, 80], "acme/demo#2": [0, 400, 80], "acme/demo#9": [400, 0, 120], "acme/demo#8": [400, 600, 80] };
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const el = this.closest<HTMLElement>("[data-ticket]");
      const [left, top, height] = natural[el?.dataset["ticket"] ?? ""] ?? [0, 0, 0];
      return DOMRect.fromRect({ x: left, y: top + (parseFloat(el?.style.marginTop ?? "") || 0), width: 260, height });
    });
    await shown();
    // #9's middle meets #1's (340), not #2's: 340 - 60 = 280.
    await waitFor(() => expect(anchor("9").style.marginTop).toBe("280px"));
    expect(edge("1", "9")?.querySelector("path")?.getAttribute("d")).toMatch(/^M 260 340 .* 400 340$/);
    expect(anchor("8").style.marginTop).toBe("");
  });

  it("offers no line onto the card's own frame or a card inside it", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(routes());
    await shown();
    await openParent(user);
    fireEvent.pointerDown(within(article("1")).getByTitle("Drag to another card: it waits for acme/demo#1"), { button: 0 });
    fireEvent.pointerMove(article("3"));
    expect(screen.queryByText(/waits for acme\/demo#1$/)).not.toBeInTheDocument();
    fireEvent.pointerUp(article("3"));
    fireEvent.pointerDown(within(article("2")).getByTitle("Drag to another card: it waits for acme/demo#2"), { button: 0 });
    fireEvent.pointerUp(anchor("1"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(fake.requests.some((r) => r.method === "PATCH")).toBe(false);
  });

  it("draws the lines behind the cards and a hovered or focused one again in front of them", async () => {
    const user = userEvent.setup();
    fakeApi(routes());
    await shown();
    await openParent(user);
    expect(inFront("4", "2")).not.toHaveClass("front");
    await user.hover(edge("4", "2") as SVGGElement);
    expect(inFront("4", "2")).toHaveClass("front");
    await user.unhover(edge("4", "2") as SVGGElement);
    expect(inFront("4", "2")).not.toHaveClass("front");
    // The keyboard reaches a line through its "×".
    within(edge("4", "2") as unknown as HTMLElement).getByRole("button").focus();
    await waitFor(() => expect(inFront("4", "2")).toHaveClass("front"));
  });

  it("removes a dependency with the line's ×, and undoes it", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(routes(undefined, { "PATCH /api/v1/repos/acme/demo/tickets/2": json(200, CARDS[2]) }));
    await shown();
    await openParent(user);
    await user.click(screen.getByRole("button", { name: "Remove the dependency: acme/demo#2 waits for acme/demo#4" }));
    expect(await screen.findByText("acme/demo#2 no longer waits for acme/demo#4.")).toBeInTheDocument();
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/2")[0]?.body).toEqual({ remove_blocked_by: [url("4")] });
    await user.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/2")[1]?.body).toEqual({ add_blocked_by: [url("4")] }));
  });

  it("shows the service's refusal as the ticket page does and leaves the figure as it was", async () => {
    const fake = fakeApi(
      routes(undefined, { "PATCH /api/v1/repos/acme/demo/tickets/1": apiError(400, "invalid", "Tickets would wait on each other forever.") }),
    );
    await shown();
    fireEvent.dragStart(draggable("1"), { dataTransfer: dataTransfer() });
    fireEvent.drop(article("4"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Tickets would wait on each other forever.");
    expect(fake.calls("PATCH", "/api/v1/repos/acme/demo/tickets/1")).toHaveLength(1);
    expect(edge("4", "1")).toHaveClass("dashed");
    expect(screen.queryByRole("button", { name: "Undo" })).not.toBeInTheDocument();
  });

  it("changes a card's lane and assignee in place, with the board's refusal", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(
      routes(undefined, {
        "PUT /api/v1/repos/acme/demo/tickets/4/board": [apiError(409, "blocked", "blocked"), json(200, CARDS[1])],
        "PUT /api/v1/repos/acme/demo/tickets/4/assignee": json(200, CARDS[1]),
      }),
    );
    await shown();
    const lane = within(article("4")).getByRole("button", { name: "Lane of acme/demo#4: New — click to change" });
    expect(lane).toHaveClass("card-control");
    await pickFromMenu(user, lane.getAttribute("aria-label") as string, "In progress", article("4"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not move acme/demo#4; it is back where it was. It can be moved to In progress once every ticket it is blocked by is closed.",
    );
    await pickFromMenu(user, "Lane of acme/demo#4: New — click to change", "Ready", article("4"));
    expect(await screen.findByText("acme/demo#4 moved to Ready.")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/board").map((r) => r.body)).toEqual([{ lane: "in_progress" }, { lane: "ready" }]);

    // The lane it is in is no move.
    await pickFromMenu(user, "Lane of acme/demo#4: New — click to change", "New", article("4"));
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/board")).toHaveLength(2);

    await pickFromMenu(user, /^Assignee of acme\/demo#4: /, "aiko", article("4"));
    await waitFor(() => expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/4/assignee")[0]?.body).toEqual({ name: "aiko" }));
  });

  it("hides unrelated tickets and lines, shows no matches, and restores the figure after clearing the filter", async () => {
    const user = userEvent.setup();
    fakeApi(routes());
    await shown();
    await user.type(screen.getByRole("textbox", { name: "Text" }), "Title of 4");
    expect(screen.getAllByRole("article")).toEqual([article("4")]);
    expect(article("4")).not.toHaveClass("dim");
    expect(edge("4", "1")).toBeNull();
    const input = screen.getByRole("textbox", { name: "Text" });
    await user.type(input, " absent");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByText("No tickets to show.")).toBeInTheDocument();
    await user.clear(input);
    expect(article("1")).toBeInTheDocument();
    expect(article("5")).toBeInTheDocument();
    expect(edge("4", "1")).toHaveClass("dashed");
  });

  it.each([false, true])("reveals a matching grandchild, permits folding, and restores saved expansion (%s)", async (savedOpen) => {
    const user = userEvent.setup();
    const saved = JSON.stringify(savedOpen ? ["acme/demo#1"] : []);
    window.localStorage.setItem("branvil.relations.open.acme", saved);
    fakeApi(routes({ open: [...CARDS.map((c) => c.key === "2" ? { ...c, children: [ref("6")] } : c), card("6", "ready", { parent: ref("2") })] }));
    await shown();
    const input = screen.getByRole("textbox", { name: "Text" });
    await user.type(input, "Title of 6");
    expect(screen.getAllByRole("article")).toEqual([article("1"), article("2"), article("6")]);
    expect(article("1")).toHaveClass("dim");
    expect(article("2")).toHaveClass("dim");
    expect(article("6")).not.toHaveClass("dim");
    expect(anchor("6").parentElement?.closest("[data-ticket]")).toBe(anchor("2"));
    await user.click(within(article("2")).getByRole("button", { name: "Close child tickets" }));
    expect(screen.queryByRole("article", { name: /^acme\/demo#6 / })).not.toBeInTheDocument();
    await user.click(within(article("2")).getByRole("button", { name: "Open child tickets" }));
    expect(article("6")).toBeInTheDocument();
    expect(window.localStorage.getItem("branvil.relations.open.acme")).toBe(saved);
    await user.clear(input);
    expect(within(article("1")).getByRole("button", { expanded: savedOpen })).toBeInTheDocument();
    expect(screen.queryByRole("article", { name: /^acme\/demo#6 / })).not.toBeInTheDocument();
    expect(article("4")).toBeInTheDocument();
    expect(article("5")).toBeInTheDocument();
  });

  it("automatically opens and filters new tickets, permits folding, and restores their previous state", async () => {
    const user = userEvent.setup();
    fakeApi(routes({ open: [...CARDS, card("6", "new"), card("7", "new")] }));
    await shown();
    const input = screen.getByRole("textbox", { name: "Text" });
    await user.type(input, "Title of 6");
    const frame = screen.getByRole("group", { name: "New tickets (1)" });
    expect(within(frame).getByRole("article")).toBe(article("6"));
    await user.click(within(frame).getByRole("button", { name: "Close child tickets" }));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    // A new filter opens the frame again.
    await user.clear(input);
    await user.type(input, "Title of 7");
    expect(article("7")).toBeInTheDocument();
    await user.clear(input);
    expect(within(screen.getByRole("group", { name: "New tickets (2)" })).getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("reveals descendants for assignee and label filters, with all filters combined", async () => {
    const user = userEvent.setup();
    fakeApi(routes({ open: CARDS.map((c) => c.key === "2" ? { ...c, assignee: { name: "owner", assigned_at: "2026-10-14T10:00:00Z" }, labels: ["bug"] } : c) }));
    await shown();
    await choose(user, "Assignee", "owner");
    expect(screen.getAllByRole("article")).toEqual([article("1"), article("2")]);
    await choose(user, "Label", "bug");
    expect(article("2")).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Text" }), "Title of 3");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("shows a failure to read the closed tickets it needs, offers to retry, and relates nothing until they are read", async () => {
    const user = userEvent.setup();
    // #8's parent #7 is closed: the closed tickets are needed, and their first reads fail.
    const open = [card("8", "ready", { parent: ref("7", "closed") })];
    const all = [card("7", "done", { state: "closed", children: [ref("8")] }), ...open];
    let failing = true;
    const fake = fakeApi(
      routes(undefined, {
        "GET /api/v1/workspaces/acme/board/cards": (req) =>
          !req.search.includes("state=all")
            ? json(200, { items: open, next_cursor: null })
            : failing
              ? apiError(500, "internal", "The board could not be read.")
              : json(200, { items: all, next_cursor: null }),
      }),
    );
    renderApp("/acme");
    // Meanwhile the open tickets are shown (#8 without its parent, so without relations), and nothing can be dragged.
    await screen.findByRole("article", { name: /^acme\/demo#8 / });
    expect(draggable("8")).toHaveAttribute("draggable", "false");
    // After the query's own retries, the failure is shown with a retry, and nothing is still "refreshing".
    const closedReads = () => fake.calls("GET", "/api/v1/workspaces/acme/board/cards").filter((r) => r.search.includes("state=all"));
    expect(await screen.findByRole("alert")).toHaveTextContent("The board could not be read.");
    expect(closedReads()).toHaveLength(3);
    expect(screen.queryByText("Refreshing…")).not.toBeInTheDocument();
    expect(draggable("8")).toHaveAttribute("draggable", "false");

    failing = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("article", { name: /^acme\/demo#7 / })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(draggable("7")).toHaveAttribute("draggable", "true");
    expect(closedReads()).toHaveLength(4);
  });

  it("reads closed tickets for a closed parent of an open one, and hides the others until asked", async () => {
    const user = userEvent.setup();
    // #7 is closed with an open child #8; #9 is closed and blocks #8.
    const open = [card("8", "ready", { parent: ref("7", "closed"), blocked_by: [ref("9", "closed")] })];
    const all = [
      card("7", "done", { state: "closed", children: [ref("8")] }),
      card("9", "done", { state: "closed", blocks: [ref("8")] }),
      ...open,
    ];
    const fake = fakeApi(routes({ open, all }));
    renderApp("/acme");
    expect(await screen.findByRole("article", { name: /^acme\/demo#7 / })).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards").some((r) => r.search.includes("state=all"))).toBe(true);
    expect(screen.queryByRole("article", { name: /^acme\/demo#9 / })).not.toBeInTheDocument();
    expect(edge("9", "7")).toBeNull();

    await user.click(screen.getByRole("checkbox", { name: "Include closed tickets" }));
    expect(await screen.findByRole("article", { name: /^acme\/demo#9 / })).toBeInTheDocument();
    expect(window.localStorage.getItem("branvil.relations.includeClosed")).toBe("true");
    expect(window.localStorage.getItem("branvil.board.includeClosed")).toBeNull();
  });
});

describe("relations: order and new tickets", () => {
  // #6 and #8 are new and not related, so in the frame of new tickets; #7 (ready) is not related either.
  const OTHERS: Card[] = [...CARDS, card("6", "new"), card("8", "new"), card("7", "ready")];
  const REPO = { path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: [], required_checks: [], labels: [], upstream: null, revision: 1 };
  const withRepo = (extra: Routes = {}) => routes({ open: OTHERS }, { "GET /api/v1/repos": json(200, { items: [REPO], next_cursor: null }), ...extra });

  /** Lays a 260×100 box at the top of the viewport under each anchor (jsdom does no layout). */
  const layOut = (...keys: string[]) => {
    for (const key of keys)
      for (const el of [anchor(key), anchor(key).querySelector(":scope > .rel-card") as HTMLElement]) el.getBoundingClientRect = () => new DOMRect(0, 0, 260, 100);
  };

  /** A drag event at a height (jsdom's events built by fireEvent.dragOver carry no coordinates). */
  const dragAt = (type: "dragover" | "drop", el: HTMLElement, clientY: number) => fireEvent(el, new MouseEvent(type, { bubbles: true, cancelable: true, clientY }));

  const freshFrame = () => screen.getByRole("group", { name: /^New tickets/ });

  /** Opens the frame of new tickets, showing #6 and #8. */
  async function others(user: ReturnType<typeof userEvent.setup>) {
    await shown();
    await user.click(within(freshFrame()).getByRole("button", { name: "Open child tickets" }));
  }

  it("keeps the new tickets without relations in a frame laid out as a new ticket, the others on their own", async () => {
    const user = userEvent.setup();
    fakeApi(withRepo());
    await shown();
    const frame = freshFrame();
    expect(frame).toHaveAccessibleName("New tickets (2)");
    // Where a new ticket goes in its column: after the ready ones, before the done ones, in the board's order.
    const column = [...(frame.parentElement?.children ?? [])].map((e) => e.getAttribute("data-ticket")?.replace("acme/demo#", ""));
    expect(column).toEqual(["7", "4", "\u0000fresh", "5"]);
    // A parent's header, in a colour of its own; closed at first.
    expect(frame.querySelector(":scope > .rel-card > .card")).toHaveClass("fresh");
    expect(within(frame).queryByRole("article")).not.toBeInTheDocument();

    await others(user);
    expect(frame).toHaveClass("rel-frame");
    expect(within(frame).getAllByRole("article").map((a) => a.getAttribute("aria-label")?.split(" ")[0])).toEqual(["acme/demo#6", "acme/demo#8"]);
    expect(within(frame).getByRole("button", { name: "Close child tickets" })).toHaveAttribute("aria-expanded", "true");
  });

  it("makes a new ticket dragged out of its frame onto the background ready, and offers no drop on the frame itself", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(withRepo({ "PUT /api/v1/repos/acme/demo/tickets/6/board": json(200, {}) }));
    await others(user);
    fireEvent.dragStart(draggable("6"), { dataTransfer: dataTransfer() });
    fireEvent.dragOver(freshFrame());
    expect(freshFrame()).not.toHaveClass("drop-target");
    const background = document.querySelector(".relations-canvas") as HTMLElement;
    fireEvent.dragOver(background);
    expect(background).toHaveClass("drop-target");
    fireEvent.drop(background);
    expect(await screen.findByText("acme/demo#6 moved to Ready.")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/6/board").map((r) => r.body)).toEqual([{ lane: "ready" }]);
  });

  it("makes a ticket without relations dropped onto the frame new, but takes no ticket with relations", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(withRepo({ "PUT /api/v1/repos/acme/demo/tickets/7/board": json(200, {}) }));
    await shown();
    await openParent(user);
    // #4 (new) and #2 (ready) are related.
    for (const key of ["4", "2"]) {
      fireEvent.dragStart(draggable(key), { dataTransfer: dataTransfer() });
      fireEvent.dragOver(freshFrame());
      expect(freshFrame()).not.toHaveClass("drop-target");
      fireEvent.dragEnd(draggable(key));
    }

    fireEvent.dragStart(draggable("7"), { dataTransfer: dataTransfer() });
    fireEvent.dragOver(freshFrame());
    expect(freshFrame()).toHaveClass("drop-target");
    fireEvent.drop(freshFrame());
    expect(await screen.findByText("acme/demo#7 moved to New.")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/7/board").map((r) => r.body)).toEqual([{ lane: "new" }]);
  });

  it("puts a card dropped near the edge of another of its lane beside it, on the board", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(withRepo({ "PUT /api/v1/repos/acme/demo/tickets/8/board": json(200, {}) }));
    await others(user);
    layOut("6", "8");
    fireEvent.dragStart(draggable("8"), { dataTransfer: dataTransfer() });
    // The middle makes a child; the top quarter puts it above.
    dragAt("dragover", article("6"), 50);
    expect(anchor("6")).toHaveClass("drop-target");
    dragAt("dragover", article("6"), 10);
    expect(anchor("6")).toHaveClass("insert-before");
    expect(anchor("6")).not.toHaveClass("refused");
    dragAt("drop", article("6"), 10);
    expect(await screen.findByText("acme/demo#8 moved to New.")).toBeInTheDocument();
    expect(fake.calls("PUT", "/api/v1/repos/acme/demo/tickets/8/board")[0]?.body).toEqual({ lane: "new", before: "acme/demo#6" });
  });

  it("refuses a gap among another lane's tickets, says which lanes go there, and keeps saying it after the drag", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(withRepo());
    await others(user);
    layOut("6", "8");
    // #7 is ready; #6 and #8 new, so is the gap between them.
    const ready = draggable("7");
    // Siblings only: move #7 into the frame of new tickets for the test.
    anchor("6").parentElement?.append(anchor("7"));
    fireEvent.dragStart(ready, { dataTransfer: dataTransfer() });
    const over = dragAt("dragover", article("8"), 10);
    expect(over).toBe(true); // not prevented: no drop here
    expect(anchor("8")).toHaveClass("insert-before", "refused");
    expect(within(anchor("8")).getByRole("status")).toHaveTextContent("Only New tickets can go here");
    fireEvent.dragEnd(ready);
    expect(within(anchor("8")).getByRole("status")).toHaveTextContent("Only New tickets can go here");
    expect(fake.requests.some((r) => r.method === "PUT")).toBe(false);
  });

  it("adds a ticket from the header and opens the frame of new tickets, where it is", async () => {
    const user = userEvent.setup();
    let created = false;
    const fake = fakeApi(
      withRepo({
        "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: created ? [...OTHERS, card("9", "new")] : OTHERS, next_cursor: null }),
        "POST /api/v1/repos/acme/demo/tickets": () => {
          created = true;
          return json(201, card("9", "new"));
        },
      }),
    );
    await shown();
    await user.click(screen.getByRole("button", { name: "Add ticket" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "A new one");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(freshFrame()).toHaveClass("rel-frame"));
    expect(fake.calls("POST", "/api/v1/repos/acme/demo/tickets")[0]?.body).toEqual({ title: "A new one", body: "", labels: [], lane: "new" });
    expect(within(freshFrame()).getByRole("article", { name: /^acme\/demo#9 / })).toBeInTheDocument();
  });

  // Every creation entry, including inserting a parent between a ticket and its parent.
  it.each([
    { item: "Add ticket", on: "6" },
    { item: "Add parent ticket", on: "2" },
    { item: "Add parent ticket", on: "5" },
    { item: "Add child ticket", on: "2" },
    { item: "Add next ticket", on: "2" },
  ])("clears all filters after successful $item on #$on and reveals the new ticket", async ({ item, on }) => {
    const user = userEvent.setup();
    const seed = OTHERS.map((c) => c.key === on ? { ...c, assignee: { name: "owner", assigned_at: "2026-10-14T10:00:00Z" }, labels: ["bug"] } : c);
    const source = seed.find((c) => c.key === on)!;
    const made = card("9", item === "Add ticket" ? "new" : "ready");
    const parent = item === "Add parent ticket";
    const child = item === "Add child ticket";
    const next = item === "Add next ticket";
    const after = [
      ...seed.map((c) => {
        if (c.key === on) return { ...c, ...(parent ? { parent: ref("9") } : child ? { children: [...c.children, ref("9")] } : next ? { blocks: [...c.blocks, ref("9")] } : {}) };
        if (parent && c.key === source.parent?.key) return { ...c, children: [...c.children.filter((r) => r.key !== on), ref("9")] };
        return c;
      }),
      { ...made, parent: parent ? source.parent : child ? ref(on) : null, children: parent ? [ref(on)] : [], blocked_by: next ? [ref(on)] : [] },
    ];
    let created = false;
    let related = false;
    const scroll = vi.spyOn(Element.prototype, "scrollIntoView");
    fakeApi(withRepo({
      "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: created ? related ? after : [...seed, made] : seed, next_cursor: null }),
      "POST /api/v1/repos/acme/demo/tickets": () => {
        created = true;
        return json(201, made);
      },
      "PATCH /api/v1/repos/acme/demo/tickets/9": () => {
        if (!parent) related = true;
        return json(200, made);
      },
      [`PATCH /api/v1/repos/acme/demo/tickets/${on}`]: () => {
        related = true;
        return json(200, source);
      },
    }));
    await shown();
    await choose(user, "Assignee", "owner");
    await choose(user, "Label", "bug");
    await user.type(screen.getByRole("textbox", { name: "Text" }), `Title of ${on}`);
    if (item === "Add ticket") {
      // A temporary fold must not prevent revealing a new card in the same frame.
      await user.click(within(freshFrame()).getByRole("button", { name: "Close child tickets" }));
      await user.click(screen.getByRole("button", { name: item }));
    }
    else await pickFromMenu(user, `Actions of acme/demo#${on}`, item, article(on));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Different title");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));

    expect(await screen.findByRole("article", { name: /^acme\/demo#9 / })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Text" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Assignee" })).toHaveValue("Assignee: Any");
    expect(screen.getByRole("combobox", { name: "Label" })).toHaveValue("Label: Any");
    // The reveal is consumed once at its final location, not left pending until filters change.
    await waitFor(() => expect(scroll.mock.instances.filter((el) => (el as Element).getAttribute("data-ticket") === "acme/demo#9")).toHaveLength(1));
    if (parent || child) expect(anchor("9").parentElement?.closest("[data-ticket]")).toBe(parent && !source.parent ? null : anchor(parent ? source.parent!.key : on));
    await user.type(screen.getByRole("textbox", { name: "Text" }), "no match");
    await user.clear(screen.getByRole("textbox", { name: "Text" }));
    expect(scroll.mock.instances.filter((el) => (el as Element).getAttribute("data-ticket") === "acme/demo#9")).toHaveLength(1);
  });

  it("keeps filters and saved expansion when creation is cancelled or fails", async () => {
    const user = userEvent.setup();
    const fake = fakeApi(withRepo({ "POST /api/v1/repos/acme/demo/tickets": apiError(409, "conflict", "Creation refused.") }));
    await shown();
    await user.type(screen.getByRole("textbox", { name: "Text" }), "Title of 2");
    await pickFromMenu(user, "Actions of acme/demo#2", "Add child ticket", article("2"));
    let dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("textbox", { name: "Text" })).toHaveValue("Title of 2");
    expect(fake.calls("POST", "/api/v1/repos/acme/demo/tickets")).toHaveLength(0);
    await pickFromMenu(user, "Actions of acme/demo#2", "Add child ticket", article("2"));
    dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Different title");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Creation refused.");
    expect(screen.getByRole("textbox", { name: "Text" })).toHaveValue("Title of 2");
    expect(window.localStorage.getItem("branvil.relations.open.acme")).toBeNull();
  });

  it("brings a new child into view only once the cards are read with its parent, opening the parent", async () => {
    const user = userEvent.setup();
    const made = card("9", "ready");
    let stage: "before" | "created" | "related" = "before";
    const cardsNow = () =>
      stage === "before"
        ? OTHERS
        : stage === "created"
          ? [...OTHERS, made]
          : [...OTHERS.map((c) => (c.key === "1" ? { ...c, children: [...c.children, ref("9")] } : c)), { ...made, parent: ref("1") }];
    fakeApi(
      withRepo({
        // The cards with the relation take a while to come: the page renders meanwhile.
        "GET /api/v1/workspaces/acme/board/cards": async () => {
          const items = cardsNow();
          if (stage === "related") await new Promise((resolve) => setTimeout(resolve, 50));
          return json(200, { items, next_cursor: null });
        },
        "POST /api/v1/repos/acme/demo/tickets": () => {
          stage = "created";
          return json(201, made);
        },
        // The cards read after the creation, with #9 on its own, are shown before the relation is written.
        "PATCH /api/v1/repos/acme/demo/tickets/9": async () => {
          await waitFor(() => expect(document.querySelector('[data-ticket="acme/demo#9"]')).not.toBeNull());
          stage = "related";
          return json(200, {});
        },
        "PUT /api/v1/repos/acme/demo/tickets/1/board": json(200, {}),
      }),
    );
    await others(user);
    await pickFromMenu(user, "Actions of acme/demo#1", "Add child ticket", article("1"));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Related");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    expect(await screen.findByText("Created acme/demo#9 as a child of acme/demo#1.")).toBeInTheDocument();
    await waitFor(() => expect(anchor("1")).toHaveClass("rel-frame"));
    expect(anchor("9").parentElement?.closest("[data-ticket]")).toBe(anchor("1"));
  });

  it("shows a new ticket on its own when its relation is refused", async () => {
    const user = userEvent.setup();
    const made = card("9", "ready");
    let created = false;
    const fake = fakeApi(
      withRepo({
        "GET /api/v1/workspaces/acme/board/cards": () => json(200, { items: created ? [...OTHERS, made] : OTHERS, next_cursor: null }),
        "POST /api/v1/repos/acme/demo/tickets": () => {
          created = true;
          return json(201, made);
        },
        "PATCH /api/v1/repos/acme/demo/tickets/9": apiError(409, "conflict", "Refused."),
      }),
    );
    await shown();
    await user.type(screen.getByRole("textbox", { name: "Text" }), "Title of 4");
    await pickFromMenu(user, "Actions of acme/demo#4", "Add child ticket", article("4"));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Related");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refused.");
    expect(await screen.findByRole("article", { name: /^acme\/demo#9 / })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Text" })).toHaveValue("");
    // Nothing is moved after the refused relation.
    expect(fake.requests.some((r) => r.method === "PUT")).toBe(false);
  });

  // #2 is ready, #4 new: related, it is made ready after the relation.
  const READY_4 = ["PUT", "4/board", { lane: "ready" }];
  it.each([
    {
      item: "Add parent ticket",
      on: "2",
      note: "Made as the parent of acme/demo#2, between it and its parent acme/demo#1.",
      done: "Created acme/demo#9 as the parent of acme/demo#2.",
      writes: [
        ["PATCH", "9", { parent: url("1") }],
        ["PATCH", "2", { parent: url("9") }],
      ],
    },
    {
      item: "Add parent ticket",
      on: "4",
      note: "Made as the parent of acme/demo#4.",
      done: "Created acme/demo#9 as the parent of acme/demo#4.",
      writes: [["PATCH", "4", { parent: url("9") }], READY_4],
    },
    {
      item: "Add child ticket",
      on: "4",
      note: "Made as a child of acme/demo#4.",
      done: "Created acme/demo#9 as a child of acme/demo#4.",
      writes: [["PATCH", "9", { parent: url("4") }], READY_4],
    },
    {
      item: "Add next ticket",
      on: "4",
      note: "Made as the next ticket after acme/demo#4: it waits for acme/demo#4.",
      done: "Created acme/demo#9, which waits for acme/demo#4.",
      writes: [["PATCH", "9", { add_blocked_by: [url("4")] }], READY_4],
    },
  ])("$item on #$on makes the new ticket ready and related to it, in order", async ({ item, on, note, done, writes }) => {
    const user = userEvent.setup();
    const fake = fakeApi(
      withRepo({
        "POST /api/v1/repos/acme/demo/tickets": json(201, card("9", "ready")),
        "PATCH /api/v1/repos/acme/demo/tickets/9": json(200, {}),
        "PATCH /api/v1/repos/acme/demo/tickets/2": json(200, {}),
        "PATCH /api/v1/repos/acme/demo/tickets/4": json(200, {}),
        "PUT /api/v1/repos/acme/demo/tickets/4/board": json(200, {}),
      }),
    );
    await shown();
    if (on === "2") await openParent(user);
    await pickFromMenu(user, `Actions of acme/demo#${on}`, item, article(on));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(note);
    await user.type(within(dialog).getByRole("textbox", { name: "Title" }), "Related");
    await user.click(within(dialog).getByRole("button", { name: "Create ticket" }));
    expect(await screen.findByText(done)).toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/repos/acme/demo/tickets")[0]?.body).toMatchObject({ lane: "ready" });
    const sent = fake.requests.filter((r) => r.method === "PATCH" || r.method === "PUT").map((r) => [r.method, r.path.replace("/api/v1/repos/acme/demo/tickets/", ""), r.body]);
    expect(sent).toEqual(writes);
  });
});
