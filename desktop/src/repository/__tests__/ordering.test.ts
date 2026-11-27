import { describe, expect, it } from "vitest";
import type { Checks } from "../api/types";
import { LANES, ticketRef } from "../api/types";
import { hasFilter, matchesFilter, NO_FILTER, rollup } from "../views/board/ordering";
import { card } from "./fixtures";

describe("card helpers", () => {
  it("names a card by its repository and number", () => {
    expect(ticketRef(card("1", "new", { repository: "acme/web" }))).toBe("acme/web#1");
  });

  it("matches a selected lane or all lanes when no filter is set", () => {
    const cards = LANES.map((lane) => card(lane, lane));
    expect(hasFilter(NO_FILTER)).toBe(false);
    expect(cards.filter((c) => matchesFilter(c, NO_FILTER))).toEqual(cards);
    for (const lane of LANES) {
      const filter = { ...NO_FILTER, lane };
      expect(hasFilter(filter)).toBe(true);
      expect(cards.filter((c) => matchesFilter(c, filter)).map((c) => c.lane)).toEqual([lane]);
    }
  });

  it("matches any of several assignees, the unassigned too, and interactive tickets by their own setting or an ancestor's", () => {
    const parent = {
      url: "u",
      repository: "acme/demo",
      key: "1",
      title: "",
      state: "open",
      state_reason: null,
    } as const;
    const cards = [
      card("a", "ready", { assignee: { name: "aiko", assigned_at: "" } }),
      card("y", "ready", { assignee: { name: "yuki", assigned_at: "" }, interactive: true }),
      card("n", "ready", { interactive_ancestor: parent }),
    ];
    const keys = (filter: typeof NO_FILTER) =>
      cards.filter((c) => matchesFilter(c, filter)).map((c) => c.key);
    expect(keys({ ...NO_FILTER, assignees: ["yuki", "aiko"] })).toEqual(["a", "y"]);
    expect(keys({ ...NO_FILTER, assignees: ["-", "yuki"] })).toEqual(["y", "n"]);
    expect(keys({ ...NO_FILTER, interactive: true })).toEqual(["y", "n"]);
    expect(keys({ ...NO_FILTER, assignees: ["aiko"], interactive: true })).toEqual([]);
    expect(hasFilter({ ...NO_FILTER, assignees: ["-"] })).toBe(true);
    expect(hasFilter({ ...NO_FILTER, interactive: true })).toBe(true);
  });

  it("never reports success for missing or unfinished checks", () => {
    const base: Checks = {
      conflicts: [],
      head_sha: "",
      base_sha: "",
      behind_by: 0,
      checks: [],
      checks_expected: true,
      base_checks: 0,
      base_failure: null,
      required_names: [],
    };
    expect(rollup(base)).toBe("not_registered");
    expect(rollup({ ...base, checks_expected: false })).toBe("none");
    expect(
      rollup({
        ...base,
        checks: [{ name: "t", status: "queued", conclusion: null, runner: null }],
      }),
    ).toBe("pending");
    expect(
      rollup({
        ...base,
        checks: [{ name: "t", status: "completed", conclusion: "startup_failure", runner: null }],
      }),
    ).toBe("failing");
    expect(
      rollup({
        ...base,
        checks: [{ name: "t", status: "completed", conclusion: "success", runner: null }],
      }),
    ).toBe("passing");
  });
});
