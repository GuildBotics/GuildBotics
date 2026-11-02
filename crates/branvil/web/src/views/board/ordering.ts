// Pure ordering logic of the kanban board (unit tested).

import { ticketRef, type Card, type Check, type CheckSet, type Lane } from "../../api/types";

// Cards are identified by their ticketRef: a board holds tickets of several repositories.

export function groupByLane(cards: Card[], lanes: readonly Lane[]): Map<Lane, Card[]> {
  const groups = new Map<Lane, Card[]>(lanes.map((l) => [l, []]));
  for (const c of cards) groups.get(c.lane)?.push(c);
  return groups;
}

/**
 * Converts a drop position among the displayed cards of a lane (which still
 * include the dragged card when it comes from this lane) into an index in the
 * lane without the dragged card.
 */
export function resolveDropIndex(laneCards: Card[], movedId: string, rawIndex: number): number {
  const from = laneCards.findIndex((c) => ticketRef(c) === movedId);
  if (from !== -1 && from < rawIndex) return rawIndex - 1;
  return rawIndex;
}

/** `before`/`after` of `PUT .../board` for `index` in a lane without the moved card. */
export function neighbors(laneWithoutMoved: Card[], index: number): { before?: string; after?: string } {
  const next = laneWithoutMoved[index];
  if (next) return { before: ticketRef(next) };
  const last = laneWithoutMoved[laneWithoutMoved.length - 1];
  return last ? { after: ticketRef(last) } : {};
}

/** Whether moving the card to (lane, index) changes nothing. */
export function isNoop(cards: Card[], movedId: string, lane: Lane, index: number): boolean {
  const moved = cards.find((c) => ticketRef(c) === movedId);
  if (!moved || moved.lane !== lane) return false;
  const laneCards = cards.filter((c) => c.lane === lane);
  return laneCards.findIndex((c) => ticketRef(c) === movedId) === index;
}

/** The optimistic card list after a move. */
export function applyMove(cards: Card[], lanes: readonly Lane[], movedId: string, lane: Lane, index: number): Card[] {
  const moved = cards.find((c) => ticketRef(c) === movedId);
  if (!moved) return cards;
  const rest = cards.filter((c) => ticketRef(c) !== movedId);
  const groups = groupByLane(rest, lanes);
  const target = groups.get(lane);
  if (!target) return cards;
  target.splice(Math.max(0, Math.min(index, target.length)), 0, { ...moved, lane });
  return [...groups.values()].flat();
}

export type CiRollup = "passing" | "failing" | "pending" | "not_registered" | "none";

const FAILED = new Set<Check["conclusion"]>(["failure", "cancelled", "timed_out", "startup_failure"]);

/** One status for a commit's checks; never reports success for missing checks. */
export function rollup(checks: CheckSet): CiRollup {
  if (checks.checks.length === 0) return checks.checks_expected ? "not_registered" : "none";
  if (checks.checks.some((c) => c.status === "completed" && FAILED.has(c.conclusion))) return "failing";
  if (checks.checks.some((c) => c.status !== "completed")) return "pending";
  return "passing";
}

export interface CardFilter {
  assignee: string; // "" any, "-" unassigned, otherwise a member name
  label: string; // "" any
  text: string;
}

export const hasFilter = (f: CardFilter): boolean => !!(f.assignee || f.label || f.text.trim());

export function matchesFilter(card: Card, f: CardFilter): boolean {
  if (f.assignee && (card.assignee?.name ?? "-") !== f.assignee) return false;
  if (f.label && !card.labels.includes(f.label)) return false;
  const text = f.text.trim().toLowerCase();
  if (text && !`${ticketRef(card)} ${card.title} ${card.body}`.toLowerCase().includes(text)) return false;
  return true;
}
