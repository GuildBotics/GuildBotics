import { describe, expect, it } from "vitest";
import type { Card, Comment, TicketRef } from "../api/types";
import { arrange, NOBODY, pickupOrders, placements, reordered, restoring, stepped, type PickupRow } from "../views/relations/pickup";
import { card } from "./fixtures";

const AGENTS = new Set(["aiko", "kenji"]);
const isAgent = (name: string) => AGENTS.has(name);

const ref = (c: Card): TicketRef => ({ url: c.url, repository: c.repository, key: c.key, title: c.title, state: c.state, state_reason: null });
const ASSIGNED_AT = "2026-11-01T00:00:00Z";
const by = (name: string) => ({ assignee: { name, assigned_at: ASSIGNED_AT } });
const comment = (author: string, created_at: string): Comment => ({ id: created_at, author, body: "", meta: null, created_at, reactions: [], url: "" });

/** A row's state in short: its number, or what keeps it. */
function said(row: PickupRow): string {
  const s = row.state;
  if (s.kind === "next") return String(s.n);
  if (s.kind === "waiting") return `waits ${s.on.join(",")}`;
  if (s.kind === "stuck") return `stuck ${s.reasons.map((r) => `${r.ticket}:${r.why}`).join(",")}`;
  return s.kind;
}

const orderOf = (cards: Card[], assignee = "aiko", comments = new Map<string, Comment[]>()) =>
  Object.fromEntries((pickupOrders(cards, isAgent, comments).get(assignee) ?? []).map((r) => [r.card.key, said(r)]));

describe("pickup order", () => {
  it("numbers ready tickets without dependencies in board order", () => {
    const cards = [card("3", "ready", by("aiko")), card("1", "ready", by("aiko")), card("2", "ready", by("aiko"))];
    const rows = pickupOrders(cards, isAgent, new Map()).get("aiko") ?? [];
    expect(rows.map((r) => [r.card.key, said(r)])).toEqual([
      ["3", "1"],
      ["1", "2"],
      ["2", "3"],
    ]);
  });

  it("skips a ticket while what it waits for is open, and numbers it in place once that is closed", () => {
    const a = card("A", "ready", by("aiko"));
    const b = card("B", "ready", { ...by("aiko"), blocked_by: [ref(a)] });
    const c = card("C", "ready", by("aiko"));
    expect(orderOf([a, b, c])).toEqual({ A: "1", B: "waits acme/demo#A", C: "2" });
    const closed = { ...a, state: "closed" as const };
    expect(orderOf([closed, { ...b, blocked_by: [ref(closed)] }, c])).toEqual({ B: "1", C: "2" });
    // Picked up (in progress) but open: still waited for.
    expect(orderOf([{ ...a, lane: "in_progress" }, b, c])).toEqual({ B: "waits acme/demo#A", C: "1" });
  });

  it("puts an agent's tickets of other parents and other repositories into one order", () => {
    const p = card("P", "in_progress");
    const q = card("Q", "in_progress");
    const cards = [
      card("1", "ready", { ...by("aiko"), parent: ref(p) }),
      card("9", "ready", { ...by("aiko"), repository: "acme/other" }),
      card("2", "ready", { ...by("aiko"), parent: ref(q) }),
    ];
    const rows = pickupOrders(cards, isAgent, new Map()).get("aiko") ?? [];
    expect(rows.map((r) => [r.ref, said(r)])).toEqual([
      ["acme/demo#1", "1"],
      ["acme/other#9", "2"],
      ["acme/demo#2", "3"],
    ]);
  });

  it("waits for tickets of other members, in progress, or of people", () => {
    const other = card("O", "ready", by("kenji"));
    const working = card("W", "in_progress", by("aiko"));
    const human = card("H", "ready", by("owner"));
    const cards = [other, working, human, card("1", "ready", { ...by("aiko"), blocked_by: [ref(other), ref(working), ref(human)] })];
    expect(orderOf(cards)).toEqual({ "1": "waits acme/demo#O,acme/demo#W,acme/demo#H" });
    // A person's tickets are in no order.
    expect(pickupOrders(cards, isAgent, new Map()).has("owner")).toBe(false);
  });

  it("says a ticket will not start when what it waits for, even further on, nobody will move", () => {
    const fresh = card("N", "new", by("aiko"));
    const nobody = card("U", "in_progress");
    const done = card("D", "done", by("aiko"));
    const waitsFresh = card("1", "ready", { ...by("aiko"), blocked_by: [ref(fresh)] });
    const cards = [
      fresh,
      nobody,
      done,
      waitsFresh,
      card("2", "ready", { ...by("aiko"), blocked_by: [ref(nobody)] }),
      card("3", "ready", { ...by("aiko"), blocked_by: [ref(done)] }),
      // Waits for one that waits for a new ticket.
      card("4", "ready", { ...by("aiko"), blocked_by: [ref(waitsFresh)] }),
      card("5", "ready", by("aiko")),
    ];
    expect(orderOf(cards)).toEqual({
      "1": "stuck acme/demo#N:new",
      "2": "stuck acme/demo#U:unassigned",
      "3": "stuck acme/demo#D:done_open",
      "4": "stuck acme/demo#N:new",
      "5": "1",
    });
  });

  it("lists the ready tickets without an assignee as not starting", () => {
    const cards = [card("1", "ready"), card("2", "ready", by("aiko"))];
    expect(orderOf(cards, NOBODY)).toEqual({ "1": "stuck acme/demo#1:unassigned" });
    expect(orderOf(cards)).toEqual({ "2": "1" });
  });

  it("skips a ticket whose last comment since the assignment is its assignee's, until someone answers", () => {
    const cards = [card("1", "ready", by("aiko")), card("2", "ready", by("aiko"))];
    const asked = new Map([["acme/demo#1", [comment("owner", "2026-11-02T00:00:00Z"), comment("aiko", "2026-11-03T00:00:00Z")]]]);
    expect(orderOf(cards, "aiko", asked)).toEqual({ "1": "answering", "2": "1" });
    const answered = new Map([["acme/demo#1", [comment("aiko", "2026-11-03T00:00:00Z"), comment("owner", "2026-11-04T00:00:00Z")]]]);
    expect(orderOf(cards, "aiko", answered)).toEqual({ "1": "1", "2": "2" });
    // The assignee's comment from before the assignment does not count.
    const before = new Map([["acme/demo#1", [comment("aiko", "2026-10-01T00:00:00Z")]]]);
    expect(orderOf(cards, "aiko", before)).toEqual({ "1": "1", "2": "2" });
    // Another agent's comment is someone else's question: the assignee takes the ticket up.
    const other = new Map([["acme/demo#1", [comment("Kenji", "2026-11-03T00:00:00Z")]]]);
    expect(orderOf(cards, "aiko", other)).toEqual({ "1": "1", "2": "2" });
    const folded = new Map([["acme/demo#1", [comment("Aiko", "2026-11-03T00:00:00Z")]]]);
    expect(orderOf(cards, "aiko", folded)).toEqual({ "1": "answering", "2": "1" });
  });

  it("numbers neither interactive tickets (their own or an ancestor's) nor those of people", () => {
    const p = card("P", "in_progress", { interactive: true });
    const cards = [
      card("1", "ready", { ...by("aiko"), interactive: true }),
      card("2", "ready", { ...by("aiko"), parent: ref(p), interactive_ancestor: ref(p) }),
      card("3", "ready", by("owner")),
      card("4", "ready", by("aiko")),
    ];
    expect(orderOf(cards)).toEqual({ "1": "interactive", "2": "interactive", "4": "1" });
  });
});

describe("reordering the pickup order", () => {
  const a = card("A", "ready", by("aiko"));
  const b = card("B", "ready", { ...by("aiko"), blocked_by: [ref(a)] });
  const c = card("C", "ready", by("aiko"));
  const d = card("D", "ready", by("aiko"));
  const cards = [a, b, c, d];
  const refs = (...keys: string[]) => keys.map((k) => `acme/demo#${k}`);
  const ready = new Set(refs("A", "B", "C", "D"));

  it("puts a ticket before another, written before it", () => {
    const moved = arrange(refs("A", "B", "C", "D"), "acme/demo#D", "acme/demo#B", cards);
    expect(moved).toEqual({ order: refs("A", "D", "B", "C"), group: refs("D") });
    expect(placements(moved.order, moved.group, ready)).toEqual({ writes: [{ ref: "acme/demo#D", at: { before: "acme/demo#B" } }] });
  });

  it("brings what it waits for (further on too) along, in order, when that would stay behind", () => {
    const e = card("E", "ready", { ...by("aiko"), blocked_by: [ref(c)] });
    const f = card("F", "ready", { ...by("aiko"), blocked_by: [ref(e)] });
    const all = [a, c, d, e, f];
    const moved = arrange(refs("A", "C", "D", "E", "F"), "acme/demo#F", "acme/demo#A", all);
    expect(moved).toEqual({ order: refs("C", "E", "F", "A", "D"), group: refs("C", "E", "F") });
    expect(placements(moved.order, moved.group, new Set(moved.order))).toEqual({
      writes: refs("C", "E", "F").map((ref) => ({ ref, at: { before: "acme/demo#A" } })),
    });
  });

  it("moves the ticket alone when what it waits for stays before it", () => {
    const moved = arrange(refs("A", "B", "C", "D"), "acme/demo#B", "acme/demo#D", cards);
    expect(moved).toEqual({ order: refs("A", "C", "B", "D"), group: refs("B") });
  });

  it("puts a ticket last after the one before it, never naming both neighbours", () => {
    const moved = arrange(refs("A", "B", "C", "D"), "acme/demo#C", null, cards);
    expect(moved.order).toEqual(refs("A", "B", "D", "C"));
    expect(placements(moved.order, moved.group, ready)).toEqual({ writes: [{ ref: "acme/demo#C", at: { after: "acme/demo#D" } }] });
    // A group put last follows itself.
    const all = [a, b, c];
    const last = arrange(refs("B", "C", "A"), "acme/demo#B", null, all);
    expect(last.order).toEqual(refs("C", "A", "B"));
    expect(placements(refs("C", "A", "B"), refs("A", "B"), ready)).toEqual({
      writes: [
        { ref: "acme/demo#A", at: { after: "acme/demo#C" } },
        { ref: "acme/demo#B", at: { after: "acme/demo#A" } },
      ],
    });
  });

  it("keeps the first of a group that is the whole order in place, the others following it", () => {
    expect(placements(refs("D", "C", "A"), refs("D", "C", "A"), ready)).toEqual({
      writes: [
        { ref: "acme/demo#C", at: { after: "acme/demo#D" } },
        { ref: "acme/demo#A", at: { after: "acme/demo#C" } },
      ],
    });
  });

  it("steps with the arrow keys past places that show the same order", () => {
    const e = card("E", "ready", by("aiko"));
    const f = card("F", "ready", { ...by("aiko"), blocked_by: [ref(e)] });
    const start = refs("A", "C", "E", "F");
    // Just before E, F pulls E along: the order it shows already.
    expect(stepped(start, "acme/demo#F", null, -1, [a, c, e, f])).toBe("acme/demo#C");
    expect(stepped(start, "acme/demo#F", null, 1, [a, c, e, f])).toBeUndefined();
    expect(stepped(start, "acme/demo#A", "acme/demo#C", -1, [a, c, e, f])).toBeUndefined();
    expect(stepped(start, "acme/demo#A", "acme/demo#C", 1, [a, c, e, f])).toBe("acme/demo#E");
  });

  it("writes nothing and names the ticket when one moved or its neighbour left the Ready lane", () => {
    const moved = arrange(refs("A", "B", "C", "D"), "acme/demo#D", "acme/demo#B", cards);
    expect(placements(moved.order, moved.group, new Set(refs("A", "C", "D")))).toEqual({ stale: "acme/demo#B" });
    expect(placements(moved.order, moved.group, new Set(refs("A", "B", "C")))).toEqual({ stale: "acme/demo#D" });
  });

  it("undoes bottom up: each just before what followed it, a moved one put back first, or the next still in the lane, or last", () => {
    const lane = refs("X", "A", "B", "Y", "C", "Z");
    // A and B followed each other: B goes back before Y, then A before B.
    expect(restoring(lane, refs("A", "B"), new Set(lane))).toEqual([
      { ref: "acme/demo#B", at: { before: "acme/demo#Y" } },
      { ref: "acme/demo#A", at: { before: "acme/demo#B" } },
    ]);
    // Y left the lane: B goes before C. Z left too: C goes last.
    expect(restoring(lane, refs("B", "C"), new Set(refs("X", "A", "B", "C")))).toEqual([
      { ref: "acme/demo#C" },
      { ref: "acme/demo#B", at: { before: "acme/demo#C" } },
    ]);
  });

  it("shows a drag before it is written: the agent's ready tickets swap places among themselves", () => {
    const other = card("O", "ready", by("kenji"));
    const shown = reordered([a, other, b, c, card("W", "in_progress", by("aiko"))], "aiko", refs("C", "A"));
    expect(shown.map((x) => x.key)).toEqual(["C", "O", "A", "B", "W"]);
    expect((pickupOrders(shown, isAgent, new Map()).get("aiko") ?? []).map((r) => r.card.key)).toEqual(["C", "A", "B"]);
  });
});
