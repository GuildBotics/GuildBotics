// The ticket updates the relations graph's drags make (unit tested).

import type { TicketPatch } from "../../api/endpoints";
import { ticketRef, type Card, type Lane, type Ticket } from "../../api/types";
import type { MessageKey } from "../../i18n/en";

/** A ticket's update, or its move to the end of a lane of its board. */
export type Change = { repo: string; key: string } & ({ patch: TicketPatch } | { lane: Lane });

export type EditMessage = Extract<
  MessageKey,
  "relations.madeChild" | "relations.unparented" | "relations.linked" | "relations.unlinked"
>;

export interface Edit {
  /** Written in order: the relation, then the moves of the new tickets it makes ready. */
  changes: Change[];
  /** The changes that write it back as it was. */
  inverse: Change[];
  message: EditMessage;
  params: Record<string, string>;
}

/** The moves of the open new tickets among `tickets` to `lane` (a closed one moved would be reopened). */
const moves = (tickets: Ticket[], lane: Lane): Change[] =>
  tickets.filter((t) => t.lane === "new" && t.state === "open").map((t) => ({ repo: t.repository, key: t.key, lane }));

/**
 * The moves that make the new tickets among `tickets` ready, as they are being
 * related or dropped on the background: only a ticket without relations is new
 * (the figure's frame of new tickets).
 */
export const readied = (tickets: Ticket[]): Change[] => moves(tickets, "ready");

function edit(
  card: Card,
  patch: TicketPatch,
  inverse: TicketPatch,
  message: EditMessage,
  params: Record<string, string>,
  readies: Card[] = [],
): Edit {
  const change = (p: TicketPatch) => ({ repo: card.repository, key: card.key, patch: p });
  return {
    changes: [change(patch), ...readied(readies)],
    inverse: [change(inverse), ...moves(readies, "new")],
    message,
    params,
  };
}

/**
 * The update for dropping a card onto `target` (null: the graph's background,
 * where a new ticket is made ready); null when it changes no relation. Cycles
 * are left to the server to refuse.
 */
export function parentEdit(dragged: Card, target: Card | null): Edit | null {
  const parent = dragged.parent;
  const child = ticketRef(dragged);
  if (!target) {
    if (!parent) return null;
    return edit(dragged, { parent: null }, { parent: parent.url }, "relations.unparented", { child, parent: ticketRef(parent) }, [dragged]);
  }
  if (ticketRef(target) === child || (parent && ticketRef(target) === ticketRef(parent))) return null;
  return edit(
    dragged,
    { parent: target.url },
    { parent: parent?.url ?? null },
    "relations.madeChild",
    { child, parent: ticketRef(target) },
    [dragged, target],
  );
}

/** The update for drawing a line from `blocker` to `blocked`; null when it changes nothing. */
export function linkEdit(blocker: Card, blocked: Card): Edit | null {
  const ref = ticketRef(blocker);
  if (ref === ticketRef(blocked) || blocked.blocked_by.some((r) => ticketRef(r) === ref)) return null;
  return dependency(blocker, blocked, true);
}

/** The update for the × on a solid line. */
export function unlinkEdit(blocker: Card, blocked: Card): Edit {
  return dependency(blocker, blocked, false);
}

function dependency(blocker: Card, blocked: Card, add: boolean): Edit {
  const on = { add_blocked_by: [blocker.url] };
  const off = { remove_blocked_by: [blocker.url] };
  return edit(
    blocked,
    add ? on : off,
    add ? off : on,
    add ? "relations.linked" : "relations.unlinked",
    { blocked: ticketRef(blocked), blocker: ticketRef(blocker) },
    add ? [blocker, blocked] : [],
  );
}
