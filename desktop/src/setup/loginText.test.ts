import { describe, expect, it } from "vitest";
import { loginText } from "./loginText";
describe("private terminal text", () => {
  it("removes CSI, OSC BEL/ST, C1 and control bytes at every chunk boundary", () => {
    const raw =
      "\x1b[94mhttps://example.test/login\x1b[0m\n\x1b]8;;https://example.test\x07CODE\x1b]8;;\x1b\\\t\x1b[2K\r\b\0\x7f\x9b31mOK\x9b0m";
    for (let split = 0; split <= raw.length; split++) {
      const clean = loginText();
      expect(clean(raw.slice(0, split)) + clean(raw.slice(split))).toBe(
        "https://example.test/login\nCODE\tOK",
      );
    }
    const clean = loginText();
    expect([...raw].map((character) => clean(character)).join("")).toBe(
      "https://example.test/login\nCODE\tOK",
    );
  });
});
