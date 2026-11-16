import { describe, expect, it } from "vitest";
import type { Card, TicketRef } from "../api/types";
import { arrange, nextPickup, pickupOrders, placements, reordered, restoring, stepped, type PickupRow } from "../views/relations/pickup";
import { card } from "./fixtures";

const AGENTS = new Set(["aiko", "kenji"]);
const isAgent = (name: string) => AGENTS.has(name);

const ref = (c: Card): TicketRef => ({ url: c.url, repository: c.repository, key: c.key, title: c.title, state: c.state, state_reason: null });
const by = (name: string) => ({ assignee: { name, assigned_at: "2026-11-01T00:00:00Z" } });

/** A row in short: its key and number, and what keeps it from starting. */
const said = (row: PickupRow) => [row.card.key, row.n, ...row.stuck.map((r) => `${r.ticket}:${r.why}`)];
const orderOf = (cards: Card[], assignee = "aiko") => (pickupOrders(cards, isAgent).get(assignee) ?? []).map(said);

describe("pickup order", () => {
  it("numbers an agent's ready tickets in board order, as the owner set them", () => {
    const cards = [card("3", "ready", by("aiko")), card("1", "ready", by("aiko")), card("2", "ready", by("aiko"))];
    expect(orderOf(cards)).toEqual([
      ["3", 1],
      ["1", 2],
      ["2", 3],
    ]);
  });

  it("numbers a ticket waiting for an open one in its place: what it waits for decides only when it starts", () => {
    const a = card("A", "ready", by("aiko"));
    const b = card("B", "ready", { ...by("aiko"), blocked_by: [ref(a)] });
    const c = card("C", "ready", by("aiko"));
    expect(orderOf([a, b, c])).toEqual([
      ["A", 1],
      ["B", 2],
      ["C", 3],
    ]);
    // Started, A leaves the order: the tickets not yet started are numbered.
    expect(orderOf([{ ...a, lane: "in_progress" }, b, c])).toEqual([
      ["B", 1],
      ["C", 2],
    ]);
  });

  it("puts an agent's tickets of other parents and other repositories into one order", () => {
    const p = card("P", "in_progress");
    const q = card("Q", "in_progress");
    const cards = [
      card("1", "ready", { ...by("aiko"), parent: ref(p) }),
      card("9", "ready", { ...by("aiko"), repository: "acme/other" }),
      card("2", "ready", { ...by("aiko"), parent: ref(q) }),
    ];
    expect((pickupOrders(cards, isAgent).get("aiko") ?? []).map((r) => [r.ref, r.n])).toEqual([
      ["acme/demo#1", 1],
      ["acme/other#9", 2],
      ["acme/demo#2", 3],
    ]);
  });

  it("marks a ticket that will not start when what it waits for, even further on, nobody will move", () => {
    const fresh = card("N", "new", by("aiko"));
    const nobody = card("U", "in_progress");
    const done = card("D", "done", by("aiko"));
    const working = card("W", "in_progress", by("kenji"));
    const waitsFresh = card("1", "ready", { ...by("aiko"), blocked_by: [ref(fresh)] });
    const cards = [
      fresh,
      nobody,
      done,
      working,
      waitsFresh,
      card("2", "ready", { ...by("aiko"), blocked_by: [ref(nobody)] }),
      card("3", "ready", { ...by("aiko"), blocked_by: [ref(done)] }),
      // Waits for one that waits for a new ticket.
      card("4", "ready", { ...by("aiko"), blocked_by: [ref(waitsFresh)] }),
      // Another member works on what it waits for: it will start.
      card("5", "ready", { ...by("aiko"), blocked_by: [ref(working)] }),
    ];
    expect(orderOf(cards)).toEqual([
      ["1", 1, "acme/demo#N:new"],
      ["2", 2, "acme/demo#U:unassigned"],
      ["3", 3, "acme/demo#D:done_open"],
      ["4", 4, "acme/demo#N:new"],
      ["5", 5],
    ]);
  });

  it("leaves out interactive tickets (their own or an ancestor's), unassigned ones, those of people, and those not ready", () => {
    const p = card("P", "in_progress", { interactive: true });
    const cards = [
      card("1", "ready", { ...by("aiko"), interactive: true }),
      card("2", "ready", { ...by("aiko"), parent: ref(p), interactive_ancestor: ref(p) }),
      card("3", "ready", by("owner")),
      card("4", "ready"),
      card("5", "new", by("aiko")),
      card("6", "ready", { ...by("aiko"), state: "closed" }),
      card("7", "ready", by("aiko")),
    ];
    const orders = pickupOrders(cards, isAgent);
    expect([...orders.keys()]).toEqual(["aiko"]);
    expect(orderOf(cards)).toEqual([["7", 1]]);
  });
});

describe("open waits and the next pickup", () => {
  it("skips an in-progress blocker without renumbering, then starts its dependent once closed", () => {
    const a = card("A", "in_progress", by("aiko"));
    const b = card("B", "ready", { ...by("aiko"), blocked_by: [ref(a)] });
    const c = card("C", "ready", by("aiko"));
    const rows = pickupOrders([a, b, c], isAgent).get("aiko")!;
    expect(rows.map((r) => r.n)).toEqual([1, 2]);
    expect(rows[0]?.waits).toEqual([{ ticket: "acme/demo#A", lane: "in_progress", assignee: "aiko" }]);
    expect(nextPickup(rows)?.card.key).toBe("C");
    const closed: Card = { ...a, state: "closed" };
    const after = pickupOrders([closed, { ...b, blocked_by: [ref(closed)] }, c], isAgent).get("aiko")!;
    expect(after[0]?.waits).toEqual([]);
    expect(nextPickup(after)?.card.key).toBe("B");
  });

  it("lists only direct open blockers, including another agent's ready ticket", () => {
    const a = card("A", "in_progress", by("aiko"));
    const b = card("B", "ready", { ...by("kenji"), blocked_by: [ref(a)] });
    const closed = card("X", "done", { state: "closed" });
    const c = card("C", "ready", { ...by("aiko"), blocked_by: [ref(b), ref(closed)] });
    const rows = pickupOrders([a, b, closed, c], isAgent).get("aiko")!;
    expect(rows[0]?.waits).toEqual([{ ticket: "acme/demo#B", lane: "ready", assignee: "kenji" }]);
    expect(nextPickup(rows)).toBeUndefined();
  });

  it("never selects stuck rows, and keeps an unloaded open blocker as a wait", () => {
    const fresh = card("N", "new", by("aiko"));
    const missing = card("M", "in_progress", by("kenji"));
    const b = card("B", "ready", { ...by("aiko"), blocked_by: [ref(fresh)] });
    const c = card("C", "ready", { ...by("aiko"), blocked_by: [ref(missing)] });
    const rows = pickupOrders([fresh, b, c], isAgent).get("aiko")!;
    expect(rows[0]?.stuck).toEqual([{ ticket: "acme/demo#N", why: "new" }]);
    expect(rows[1]?.waits).toEqual([{ ticket: "acme/demo#M", lane: null, assignee: null }]);
    expect(nextPickup(rows)).toBeUndefined();
    expect(nextPickup([])).toBeUndefined();
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
    // B was started meanwhile: it stays where it is, and A goes before Y.
    expect(restoring(lane, refs("A", "B"), new Set(refs("X", "A", "Y", "C", "Z")))).toEqual([{ ref: "acme/demo#A", at: { before: "acme/demo#Y" } }]);
  });

  it("shows a drag before it is written: the agent's numbered tickets swap places among themselves", () => {
    const other = card("O", "ready", by("kenji"));
    const talk = card("T", "ready", { ...by("aiko"), interactive: true });
    const shown = reordered([a, other, talk, b, c, card("W", "in_progress", by("aiko"))], "aiko", refs("C", "A"));
    expect(shown.map((x) => x.key)).toEqual(["C", "O", "T", "A", "B", "W"]);
    expect((pickupOrders(shown, isAgent).get("aiko") ?? []).map((r) => r.card.key)).toEqual(["C", "A", "B"]);
  });
});
