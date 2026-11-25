// The order in which the owner has agents start their ready tickets, and the
// board writes that reorder it (unit tested).
//
// An agent takes up its ready tickets from the top of the Ready lane (its
// rank): the agent module
// (`crates/branvil/src/agent/patrol.rs`, `ticket_candidates`).
// The owner sets that order; when a ticket that is not yet started then waits
// for one another member is working on, the system decides when it starts.

import { ticketRef, type Card, type Lane } from "../../api/types";

/** Why a ticket will not start as things are: a ticket it waits for, and what keeps that one. */
export interface Stuck {
  ticket: string;
  why: "new" | "unassigned" | "done_open";
}

/** A direct open blocker, with its board details when loaded. */
export type Wait = { ticket: string } | { ticket: string; lane: Lane; assignee: string | null };

/** A ticket of an agent's pickup order: its number (from 1, in board order), and what keeps it from ever starting. */
export interface PickupRow {
  ref: string;
  card: Card;
  n: number;
  stuck: Stuck[];
  waits: Wait[];
}

/** Where a ticket is put in its lane: just before or after another ticket. */
export type Place = { before: string } | { after: string };

/** A ticket's direct open blockers, including those not loaded on the board. */
const openBlockers = (card: Card) =>
  card.blocked_by.filter((b) => b.state === "open").map(ticketRef);

/** The open tickets a ticket waits for, following what those wait for in turn. */
function waitedFor(start: string, byRef: ReadonlyMap<string, Card>): string[] {
  const seen = new Set<string>();
  const visit = (ref: string) => {
    const card = byRef.get(ref);
    for (const b of card ? openBlockers(card) : []) {
      if (seen.has(b) || b === start) continue;
      seen.add(b);
      visit(b);
    }
  };
  visit(start);
  return [...seen];
}

/** What keeps a ticket from ever closing: nobody will move on it. */
function stuckOf(card: Card): Stuck["why"] | null {
  if (card.state !== "open") return null;
  if (card.lane === "new") return "new";
  if (!card.assignee) return "unassigned";
  // An agent moving it to Done does not close it: it waits for a person.
  if (card.lane === "done") return "done_open";
  return null;
}

/** An agent's tickets in its pickup order: open, ready, and not left to a person (interactive). */
const ordered = (card: Card, assignee: string) =>
  card.state === "open" &&
  card.lane === "ready" &&
  card.assignee?.name === assignee &&
  !card.interactive &&
  !card.interactive_ancestor;

/**
 * The pickup order of each agent: its open ready tickets that are not
 * interactive, in board order, numbered from 1. `cards` are the board's,
 * before any filter, in board order.
 */
export function pickupOrders(
  cards: readonly Card[],
  isAgent: (name: string) => boolean,
): Map<string, PickupRow[]> {
  const byRef = new Map(cards.map((c) => [ticketRef(c), c]));
  const orders = new Map<string, PickupRow[]>();
  for (const card of cards) {
    const assignee = card.assignee?.name;
    if (!assignee || !isAgent(assignee) || !ordered(card, assignee)) continue;
    const ref = ticketRef(card);
    const rows = orders.get(assignee) ?? [];
    orders.set(assignee, rows);
    const stuck = waitedFor(ref, byRef).flatMap((b) => {
      const blocker = byRef.get(b);
      const why = blocker && stuckOf(blocker);
      return why ? [{ ticket: b, why }] : [];
    });
    const waits = openBlockers(card).map((ticket) => {
      const blocker = byRef.get(ticket);
      return blocker
        ? { ticket, lane: blocker.lane, assignee: blocker.assignee?.name ?? null }
        : { ticket };
    });
    rows.push({ ref, card, n: rows.length + 1, stuck, waits });
  }
  return orders;
}

/** The first row that can start in the displayed order, including a drag preview. */
export const nextPickup = (rows: readonly PickupRow[]): PickupRow | undefined =>
  rows.find((row) => row.waits.length === 0 && row.stuck.length === 0);

/**
 * `order` with `moved` put just before `before` (null: last). The tickets of
 * `order` it waits for (following what those wait for) that would stay after
 * it come along, in their order, just before it: left behind, they would keep
 * it from starting where it was put. `group` is what moved, in its new order.
 */
export function arrange(
  order: readonly string[],
  moved: string,
  before: string | null,
  cards: readonly Card[],
): { order: string[]; group: string[] } {
  const byRef = new Map(cards.map((c) => [ticketRef(c), c]));
  const rest = order.filter((r) => r !== moved);
  const at = before === null ? rest.length : Math.max(0, rest.indexOf(before));
  const after = rest.slice(at);
  const waited = new Set(waitedFor(moved, byRef).filter((r) => after.includes(r)));
  const group = [...after.filter((r) => waited.has(r)), moved];
  const stays = rest.filter((r) => !waited.has(r));
  const anchor = after.find((r) => !waited.has(r));
  const index = anchor === undefined ? stays.length : stays.indexOf(anchor);
  return { order: [...stays.slice(0, index), ...group, ...stays.slice(index)], group };
}

/**
 * Where a moved row goes next with the arrow keys (`step` -1 up, 1 down): the
 * nearest place among the rows it started from that shows another order. A
 * place only pulling along what it waits for shows the same order, and is passed.
 * Undefined: nowhere else.
 */
export function stepped(
  start: readonly string[],
  moved: string,
  before: string | null,
  step: -1 | 1,
  cards: readonly Card[],
): string | null | undefined {
  const rest = start.filter((r) => r !== moved);
  const shown = arrange(start, moved, before, cards).order;
  for (
    let at = (before === null ? rest.length : rest.indexOf(before)) + step;
    at >= 0 && at <= rest.length;
    at += step
  ) {
    const target = rest[at] ?? null;
    if (arrange(start, moved, target, cards).order.some((r, i) => r !== shown[i])) return target;
  }
  return undefined;
}

/**
 * The board writes that put `group` where it stands in `order`, written in
 * turn: before the ticket after it, or (last) after the one before it. Only
 * one of `before` and `after` is given: with both, the service refuses tickets
 * that are not next to each other, and other members' tickets lie between.
 * `ready` are the open tickets of the Ready lane now: a ticket of the group, or
 * its neighbour, that left it is named instead (`stale`), and nothing is written.
 */
export function placements(
  order: readonly string[],
  group: readonly string[],
  ready: ReadonlySet<string>,
): { writes: { ref: string; at: Place }[] } | { stale: string } {
  const first = order.indexOf(group[0] ?? "");
  const next = order[first + group.length];
  const previous = first > 0 ? order[first - 1] : undefined;
  const neighbour = next ?? previous;
  const stale = [...group, ...(neighbour ? [neighbour] : [])].find((r) => !ready.has(r));
  if (stale) return { stale };
  if (next) return { writes: group.map((ref) => ({ ref, at: { before: next } })) };
  // The group is the whole order: its first ticket stays, the others follow it.
  if (!previous)
    return { writes: group.slice(1).map((ref, i) => ({ ref, at: { after: group[i] as string } })) };
  return {
    writes: group.map((ref, i) => ({
      ref,
      at: { after: i === 0 ? previous : (group[i - 1] as string) },
    })),
  };
}

/**
 * The board writes that put `moved` back where they stood in `lane` (the
 * Ready lane's tickets when they were moved), bottom up: each just before the
 * ticket that followed it, or the next after that one still in the lane
 * (`ready`, the open Ready tickets when it is undone), or last. A moved ticket
 * that followed is put back first, so it stands where it did when the one
 * above it is put before it. One that has left the lane since stays where it
 * is: putting it back would undo its start.
 */
export function restoring(
  lane: readonly string[],
  moved: readonly string[],
  ready: ReadonlySet<string>,
): { ref: string; at?: Place }[] {
  const back = moved
    .filter((r) => ready.has(r))
    .toSorted((a, b) => lane.indexOf(b) - lane.indexOf(a));
  return back.map((ref) => {
    const next = lane.slice(lane.indexOf(ref) + 1).find((r) => ready.has(r));
    return next ? { ref, at: { before: next } } : { ref };
  });
}

/**
 * The board's cards with an agent's pickup order in `order` (a drag shown
 * before it is written): its tickets swap places among themselves, every other
 * card stays. Tickets `order` does not name keep their order after it.
 */
export function reordered(
  cards: readonly Card[],
  assignee: string,
  order: readonly string[],
): Card[] {
  const mine = (c: Card) => ordered(c, assignee);
  const own = cards.filter(mine);
  const byRef = new Map(own.map((c) => [ticketRef(c), c]));
  const named = order.flatMap((r) => byRef.get(r) ?? []);
  const queue = [...named, ...own.filter((c) => !named.includes(c))];
  return cards.map((c) => (mine(c) ? (queue.shift() as Card) : c));
}
