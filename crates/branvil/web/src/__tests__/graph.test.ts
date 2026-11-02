import { describe, expect, it } from "vitest";
import type { Card, TicketRef } from "../api/types";
import { buildGraph, type Graph, type GraphNode, type Level } from "../views/relations/graph";
import type { CardFilter } from "../views/board/ordering";
import { card } from "./fixtures";

interface Spec {
  parent?: string;
  blockedBy?: string[];
  closed?: boolean;
}

/** Cards in board order, each relation on both ends as the service gives it; a key not in `specs` names a ticket that is not loaded. */
function board(specs: Record<string, Spec>): Card[] {
  const cards = Object.entries(specs).map(([key, s]) => card(key, "new", s.closed ? { state: "closed" } : {}));
  const ref = (key: string): TicketRef => {
    const c = cards.find((c) => c.key === key) ?? card(key, "new");
    return { url: c.url, repository: c.repository, key, title: c.title, state: c.state, state_reason: null };
  };
  const loaded = (key: string) => cards.find((c) => c.key === key);
  for (const c of cards) {
    const s = specs[c.key]!;
    c.parent = s.parent ? ref(s.parent) : null;
    c.blocked_by = (s.blockedBy ?? []).map(ref);
    if (s.parent) loaded(s.parent)?.children.push(ref(c.key));
    for (const b of s.blockedBy ?? []) loaded(b)?.blocks.push(ref(c.key));
  }
  return cards;
}

const graph = (specs: Record<string, Spec>, open: string[] = [], includeClosed = false) =>
  buildGraph(board(specs), { includeClosed, open: new Set(open.map((k) => `acme/demo#${k}`)) });

const short = (ref: string) => ref.replace("acme/demo#", "");

/** Columns separated by `|`, an open parent's level in brackets, the frame of new tickets in braces. */
const layout = (level: Level): string =>
  level
    .map((col) => col.map((n) => ("fresh" in n ? `{${n.fresh.map((c) => c.key).join(" ")}}` : short(n.ref) + (n.inner ? `[${layout(n.inner)}]` : ""))).join(" "))
    .join(" | ");

/** The node of a ticket at a column and row. */
const at = (level: Level, column: number, row: number) => level[column]![row] as GraphNode;

/** `a>b` for a solid line, `a..>b` for a dashed one. */
const lines = (g: Graph) => g.edges.map((e) => `${short(e.from)}${e.dashed ? "..>" : ">"}${short(e.to)}`);

describe("relations graph", () => {
  const textFilter = (text: string): CardFilter => ({ text, assignee: "", label: "" });

  it("filters before layout, opens all ancestors of matches, and removes unrelated branches and dependencies", () => {
    const cards = board({ P: {}, Q: { parent: "P" }, R: { parent: "Q", blockedBy: ["B"] }, S: { parent: "P" }, B: {}, X: { blockedBy: ["R"] } });
    const g = buildGraph(cards, { includeClosed: false, open: new Set(), filter: textFilter("Title of R") });
    expect(layout(g.level)).toBe("P[Q[R]]");
    expect(g.edges).toEqual([]);
    expect(at(at(at(g.level, 0, 0).inner!, 0, 0).inner!, 0, 0)).toMatchObject({ hasChildren: false });
  });

  it("does not retain nonmatching children of a matching parent, or unrelated new tickets", () => {
    const g = buildGraph(board({ P: {}, Q: { parent: "P" }, A: {} }), { includeClosed: false, open: new Set(["acme/demo#P"]), filter: textFilter("Title of P") });
    expect(layout(g.level)).toBe("P");
    expect(at(g.level, 0, 0)).toMatchObject({ hasChildren: false, inner: null });
  });

  it.each([
    { assignee: "agent-one", label: "", text: "" },
    { assignee: "", label: "bug", text: "" },
    { assignee: "", label: "", text: "  NEEDLE  " },
    { assignee: "", label: "", text: "acme/demo#R" },
    { assignee: "agent-one", label: "bug", text: "needle" },
  ])("uses the board's filter semantics to find descendants: %j", (filter) => {
    const cards = board({ P: {}, Q: { parent: "P" }, R: { parent: "Q" }, S: { parent: "P" } });
    Object.assign(cards.find((c) => c.key === "R")!, { assignee: { name: "agent-one", assigned_at: "2026-10-14T10:00:00Z" }, labels: ["bug"], body: "Needle in the body" } satisfies Partial<Card>);
    const g = buildGraph(cards, { includeClosed: false, open: new Set(), filter });
    expect(layout(g.level)).toBe("P[Q[R]]");
    expect(buildGraph(cards, { includeClosed: false, open: new Set(), filter: { ...filter, label: "absent" } }).level).toEqual([]);
  });

  it("applies filters within the closed-ticket scope, including closed ancestors but not hidden closed matches", () => {
    const cards = board({ P: { closed: true }, R: { parent: "P" }, X: { parent: "P", closed: true } });
    expect(layout(buildGraph(cards, { includeClosed: false, open: new Set(), filter: textFilter("Title of R") }).level)).toBe("P[R]");
    expect(buildGraph(cards, { includeClosed: false, open: new Set(), filter: textFilter("Title of X") }).level).toEqual([]);
    expect(layout(buildGraph(cards, { includeClosed: true, open: new Set(), filter: textFilter("Title of X") }).level)).toBe("P[X]");
  });

  it("keeps direct lines between visible matches and gathers them when an automatically opened parent is manually folded", () => {
    const cards = board({ P: {}, R: { parent: "P" }, X: { blockedBy: ["R"] } });
    for (const c of cards.filter((c) => c.key !== "P")) c.labels = ["bug"];
    const options = { includeClosed: false, open: new Set<string>(), filter: { ...textFilter(""), label: "bug" } };
    const expanded = buildGraph(cards, options);
    expect(layout(expanded.level)).toBe("P[R] | X");
    expect(lines(expanded)).toEqual(["R>X"]);
    const folded = buildGraph(cards, { ...options, openOverrides: new Map([["acme/demo#P", false]]) });
    expect(layout(folded.level)).toBe("P | X");
    expect(lines(folded)).toEqual(["P..>X"]);
  });

  it("leaves expansion unchanged for empty or whitespace-only filters, ignoring filter-time overrides", () => {
    const cards = board({ P: {}, R: { parent: "P" }, X: {} });
    const options = { includeClosed: false, open: new Set<string>() };
    expect(buildGraph(cards, { ...options, filter: textFilter("  "), openOverrides: new Map([["acme/demo#P", true]]) })).toEqual(buildGraph(cards, options));
  });

  it("puts a ticket in the column of its longest chain of blockers, in board order within a column", () => {
    const g = graph({
      C: { blockedBy: ["A", "B", "D"] },
      D: {},
      B: { blockedBy: ["A"] },
      A: {},
      E: {},
    });
    expect(layout(g.level)).toBe("D A {E} | B | C");
    expect(lines(g)).toEqual(["A>C", "B>C", "D>C", "A>B"]);
    const solid = g.edges[0]!;
    expect([solid.blocker?.key, solid.blocked?.key]).toEqual(["A", "C"]);
  });

  it("puts the lanes that matter most first in a column, keeping the board's order within a lane", () => {
    const lanes: Record<string, Card["lane"]> = { A: "new", B: "ready", C: "new", D: "done", E: "in_progress", F: "ready", Z: "new" };
    const cards = board({ A: {}, B: {}, C: {}, D: {}, E: {}, F: {}, Z: { blockedBy: ["A", "B", "C", "D", "E", "F"] } }).map((c) => ({ ...c, lane: lanes[c.key]! }));
    const g = buildGraph(cards, { includeClosed: false, open: new Set() });
    expect(layout(g.level)).toBe("E B F A C D | Z");
  });

  it("gathers the new tickets with no relation into one frame, counting relations to tickets not shown or not loaded", () => {
    // B waits for Z and A is a child of Y, neither loaded; D is the parent of a hidden closed child E,
    // F waits for it; only C and G have no relation.
    const g = graph({ B: { blockedBy: ["Z"] }, A: { parent: "Y" }, C: {}, D: {}, E: { parent: "D", closed: true }, F: { blockedBy: ["E"] }, G: {} });
    expect(layout(g.level)).toBe("B A {C G} D F");
    expect(g.edges).toEqual([]);
    expect(at(g.level, 0, 3)).toMatchObject({ hasChildren: false });
  });

  it("lays out the other tickets with no relation, and the frame where its first ticket would be, by lane", () => {
    // R, N, M and its child K are related; the others are not. On the board N comes before
    // the new ones without relations, and M between them: the frame is where A would be.
    const lanes: Record<string, Card["lane"]> = { A: "new", N: "new", M: "new", K: "ready", B: "ready", C: "new", D: "done", E: "in_progress", R: "ready" };
    const cards = board({ N: { blockedBy: ["R"] }, A: {}, M: {}, K: { parent: "M" }, B: {}, C: {}, D: {}, E: {}, R: {} }).map((c) => ({ ...c, lane: lanes[c.key]! }));
    const g = buildGraph(cards, { includeClosed: false, open: new Set() });
    expect(layout(g.level)).toBe("E B R {A C} M D | N");
    // A ticket without children has nothing to open.
    expect(at(g.level, 0, 0)).toMatchObject({ hasChildren: false, inner: null });
  });

  it("hides closed tickets and their lines unless asked, but keeps closed ancestors of open tickets", () => {
    const specs: Record<string, Spec> = {
      A: { blockedBy: ["X"] },
      X: { closed: true },
      G: { closed: true },
      P: { parent: "G", closed: true },
      C: { parent: "P" },
      Q: { closed: true },
      R: { parent: "Q", closed: true },
    };
    const hidden = graph(specs, ["G", "P"]);
    expect(layout(hidden.level)).toBe("A G[P[C]]");
    expect(lines(hidden)).toEqual([]);

    const all = graph(specs, ["G", "P"], true);
    expect(layout(all.level)).toBe("X G[P[C]] Q | A");
    expect(lines(all)).toEqual(["X>A"]);
  });

  it("gathers a closed parent's descendants' dependencies into one dashed line, solid winning over dashed", () => {
    const specs: Record<string, Spec> = {
      P: {},
      A: { parent: "P", blockedBy: ["B"] },
      B: { parent: "P" },
      X: { blockedBy: ["A", "B"] },
    };
    const g = graph(specs, ["X"]);
    expect(layout(g.level)).toBe("P | X");
    expect(lines(g)).toEqual(["P..>X"]); // no line inside P
    expect(g.edges[0]?.blocker).toBeUndefined();
    expect(g.level[0]![0]).toMatchObject({ hasChildren: true, inner: null });
    // A ticket without children has nothing to open.
    expect(g.level[1]![0]).toMatchObject({ hasChildren: false, inner: null });

    const own = graph({ ...specs, X: { blockedBy: ["A", "P", "B"] } });
    expect(lines(own)).toEqual(["P>X"]);
    expect(own.edges[0]?.blocker?.key).toBe("P");
  });

  it("lays out an open parent's children inside it and connects them directly across the frame", () => {
    const g = graph(
      {
        X: { blockedBy: ["B"] },
        P: { blockedBy: ["Y"] },
        B: { parent: "P", blockedBy: ["A"] },
        A: { parent: "P" },
        Y: {},
      },
      ["P"],
    );
    expect(layout(g.level)).toBe("Y | P[A | B] | X");
    expect(lines(g)).toEqual(["B>X", "Y>P", "A>B"]);
    expect(g.edges.every((e) => !e.dashed)).toBe(true);
  });

  it("gathers a grandchild into its topmost closed ancestor", () => {
    const specs: Record<string, Spec> = {
      P: {},
      Q: { parent: "P" },
      R: { parent: "Q" },
      X: { blockedBy: ["R"] },
    };
    const outer = graph(specs, ["P"]);
    expect(layout(outer.level)).toBe("P[Q] | X");
    expect(at(at(outer.level, 0, 0).inner!, 0, 0)).toMatchObject({ hasChildren: true, inner: null });
    expect(lines(outer)).toEqual(["Q..>X"]);

    expect(lines(graph(specs))).toEqual(["P..>X"]);
    expect(lines(graph(specs, ["Q"]))).toEqual(["P..>X"]);
    expect(layout(graph(specs, ["P", "Q"]).level)).toBe("P[Q[R]] | X");
  });

  it("settles columns of a cycle of gathered dependencies by board order", () => {
    // Legal on the server: A waits for C, D waits for B.
    const specs: Record<string, Spec> = {
      P: {},
      Q: {},
      A: { parent: "P", blockedBy: ["C"] },
      B: { parent: "P" },
      C: { parent: "Q" },
      D: { parent: "Q", blockedBy: ["B"] },
    };
    const g = graph(specs);
    expect(layout(g.level)).toBe("Q | P");
    expect(lines(g)).toEqual(["Q..>P", "P..>Q"]);

    const { P, Q, ...rest } = specs;
    expect(layout(graph({ Q: Q!, P: P!, ...rest }).level)).toBe("P | Q");
  });

  it("draws no line between a ticket and its ancestor", () => {
    const g = graph({ P: { blockedBy: ["R"] }, Q: { parent: "P" }, R: { parent: "Q" } }, ["P", "Q"]);
    expect(layout(g.level)).toBe("P[Q[R]]");
    expect(g.edges).toEqual([]);
  });
});
