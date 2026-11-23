import { describe, expect, it } from "vitest";
import type { Checks } from "../api/types";
import { ticketRef } from "../api/types";
import { rollup } from "../views/board/ordering";
import { card } from "./fixtures";

describe("card helpers", () => {
  it("names a card by its repository and number", () => {
    expect(ticketRef(card("1", "new", { repository: "acme/web" }))).toBe("acme/web#1");
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
