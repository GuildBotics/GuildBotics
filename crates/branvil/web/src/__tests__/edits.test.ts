import { describe, expect, it } from "vitest";
import type { Card, TicketRef } from "../api/types";
import { linkEdit, parentEdit, readied, unlinkEdit } from "../views/relations/edits";
import { card } from "./fixtures";

const ref = (c: Card): TicketRef => ({ url: c.url, repository: c.repository, key: c.key, title: c.title, state: c.state, state_reason: null });

const url = (key: string) => `${window.location.origin}/acme/demo/-/tickets/${key}`;

const lane = (key: string, to: string) => ({ repo: "acme/demo", key, lane: to });

describe("relations edits", () => {
  const parent = card("1", "ready");
  const other = card("2", "ready");
  const orphan = card("3", "ready");
  const child = card("4", "ready", { parent: ref(parent) });

  it("makes the dragged card a child of the target, undone by taking it out again", () => {
    expect(parentEdit(orphan, other)).toEqual({
      changes: [{ repo: "acme/demo", key: "3", patch: { parent: url("2") } }],
      inverse: [{ repo: "acme/demo", key: "3", patch: { parent: null } }],
      message: "relations.madeChild",
      params: { child: "acme/demo#3", parent: "acme/demo#2" },
    });
  });

  it("moves a child to another parent, undone by moving it back", () => {
    expect(parentEdit(child, other)).toEqual({
      changes: [{ repo: "acme/demo", key: "4", patch: { parent: url("2") } }],
      inverse: [{ repo: "acme/demo", key: "4", patch: { parent: url("1") } }],
      message: "relations.madeChild",
      params: { child: "acme/demo#4", parent: "acme/demo#2" },
    });
  });

  it("takes a child out of its parent when dropped on the background", () => {
    expect(parentEdit(child, null)).toEqual({
      changes: [{ repo: "acme/demo", key: "4", patch: { parent: null } }],
      inverse: [{ repo: "acme/demo", key: "4", patch: { parent: url("1") } }],
      message: "relations.unparented",
      params: { child: "acme/demo#4", parent: "acme/demo#1" },
    });
  });

  it("changes nothing on itself, on its current parent, or on the background without a parent", () => {
    expect(parentEdit(orphan, orphan)).toBeNull();
    expect(parentEdit(child, parent)).toBeNull();
    expect(parentEdit(orphan, null)).toBeNull();
    // The same number in another repository is another ticket.
    expect(parentEdit(child, card("1", "ready", { repository: "acme/web" }))?.changes[0]).toMatchObject({
      patch: { parent: `${window.location.origin}/acme/web/-/tickets/1` },
    });
  });

  it("makes the ticket a line is drawn to wait for the one it starts from", () => {
    expect(linkEdit(parent, other)).toEqual({
      changes: [{ repo: "acme/demo", key: "2", patch: { add_blocked_by: [url("1")] } }],
      inverse: [{ repo: "acme/demo", key: "2", patch: { remove_blocked_by: [url("1")] } }],
      message: "relations.linked",
      params: { blocked: "acme/demo#2", blocker: "acme/demo#1" },
    });
  });

  it("draws no line onto itself or onto a ticket already waiting", () => {
    expect(linkEdit(parent, parent)).toBeNull();
    expect(linkEdit(parent, card("2", "ready", { blocked_by: [ref(parent)] }))).toBeNull();
  });

  it("removes a dependency, undone by adding it back", () => {
    expect(unlinkEdit(parent, other)).toEqual({
      changes: [{ repo: "acme/demo", key: "2", patch: { remove_blocked_by: [url("1")] } }],
      inverse: [{ repo: "acme/demo", key: "2", patch: { add_blocked_by: [url("1")] } }],
      message: "relations.unlinked",
      params: { blocked: "acme/demo#2", blocker: "acme/demo#1" },
    });
  });

  describe("only a ticket without relations is new", () => {
    const fresh = card("5", "new");
    const another = card("6", "new");
    const closed = card("7", "new", { state: "closed" });

    it("makes the new tickets a relation is made between ready after it, and new again when it is undone", () => {
      expect(parentEdit(fresh, another)).toMatchObject({
        changes: [{ key: "5", patch: { parent: url("6") } }, lane("5", "ready"), lane("6", "ready")],
        inverse: [{ key: "5", patch: { parent: null } }, lane("5", "new"), lane("6", "new")],
      });
      expect(parentEdit(child, fresh)).toMatchObject({ changes: [{ key: "4" }, lane("5", "ready")], inverse: [{ key: "4" }, lane("5", "new")] });
      expect(linkEdit(fresh, other)).toMatchObject({ changes: [{ key: "2" }, lane("5", "ready")], inverse: [{ key: "2" }, lane("5", "new")] });
      expect(linkEdit(other, fresh)).toMatchObject({ changes: [{ key: "5" }, lane("5", "ready")], inverse: [{ key: "5" }, lane("5", "new")] });
    });

    it("makes a new child taken out onto the background ready, and new again in its parent when it is undone", () => {
      const inNew = card("8", "new", { parent: ref(parent) });
      expect(parentEdit(inNew, null)).toMatchObject({
        changes: [{ key: "8", patch: { parent: null } }, lane("8", "ready")],
        inverse: [{ key: "8", patch: { parent: url("1") } }, lane("8", "new")],
      });
    });

    it("moves no lane when a dependency is removed, nor for a closed ticket (moving it would reopen it)", () => {
      const inNew = card("8", "new", { blocked_by: [ref(other)] });
      expect(unlinkEdit(other, inNew).changes).toHaveLength(1);
      expect(parentEdit(closed, other)?.changes).toHaveLength(1);
      expect(parentEdit(card("9", "new", { state: "closed", parent: ref(parent) }), null)?.changes).toHaveLength(1);
      expect(readied([closed, fresh, other])).toEqual([lane("5", "ready")]);
    });
  });
});
