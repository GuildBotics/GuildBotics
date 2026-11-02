import { describe, expect, it } from "vitest";
import type { Checks } from "../api/types";
import { ticketRef } from "../api/types";
import { applyMove, isNoop, neighbors, resolveDropIndex, rollup } from "../views/board/ordering";
import { card } from "./fixtures";

const LANES = ["new", "ready", "in_progress", "done"] as const;

describe("board ordering", () => {
  const cards = [card("1", "new"), card("2", "new"), card("1", "new", { repository: "acme/web" }), card("4", "ready")];

  it("names a card by its repository and number", () => {
    expect(ticketRef(card("1", "new", { repository: "acme/web" }))).toBe("acme/web#1");
  });

  it("computes before/after for a position as `<repository>#<number>`", () => {
    // Numbers repeat across the repositories of one board.
    const lane = [card("2", "new"), card("2", "new", { repository: "acme/web" })];
    expect(neighbors(lane, 0)).toEqual({ before: "acme/demo#2" });
    expect(neighbors(lane, 1)).toEqual({ before: "acme/web#2" });
    expect(neighbors(lane, 2)).toEqual({ after: "acme/web#2" });
    expect(neighbors([], 0)).toEqual({});
  });

  it("converts a drop position that still counts the dragged card", () => {
    const lane = cards.slice(0, 3);
    expect(resolveDropIndex(lane, ticketRef(lane[0]!), 2)).toBe(1); // acme/demo#1 dropped between #2 and acme/web#1
    expect(resolveDropIndex(lane, ticketRef(lane[2]!), 0)).toBe(0); // acme/web#1 dropped on top
    expect(resolveDropIndex([], "acme/demo#9", 0)).toBe(0);
  });

  it("applies a move optimistically and detects no-ops", () => {
    const moved = applyMove(cards, LANES, "acme/demo#1", "ready", 0);
    expect(moved.map((c) => `${c.lane}:${ticketRef(c)}`)).toEqual(["new:acme/demo#2", "new:acme/web#1", "ready:acme/demo#1", "ready:acme/demo#4"]);
    expect(isNoop(cards, "acme/demo#2", "new", 1)).toBe(true);
    expect(isNoop(cards, "acme/demo#2", "new", 0)).toBe(false);
  });

  it("never reports success for missing or unfinished checks", () => {
    const base: Checks = { head_sha: "", base_sha: "", behind_by: 0, checks: [], checks_expected: true, base_checks: 0 };
    expect(rollup(base)).toBe("not_registered");
    expect(rollup({ ...base, checks_expected: false })).toBe("none");
    expect(rollup({ ...base, checks: [{ name: "t", status: "queued", conclusion: null }] })).toBe("pending");
    expect(rollup({ ...base, checks: [{ name: "t", status: "completed", conclusion: "startup_failure" }] })).toBe("failing");
    expect(rollup({ ...base, checks: [{ name: "t", status: "completed", conclusion: "success" }] })).toBe("passing");
  });
});
