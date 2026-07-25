import { describe, expect, it } from "vitest";

import {
  acceleratorFromKeyPress,
  formatAccelerator,
  hasEnoughModifiers,
  isFunctionKey,
  type KeyPress,
} from "./accelerator";

function press(code: string, modifiers: Partial<Omit<KeyPress, "code">> = {}): KeyPress {
  return {
    code,
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    ...modifiers,
  };
}

describe("acceleratorFromKeyPress", () => {
  it("names the physical letter regardless of what the modifier composes", () => {
    expect(acceleratorFromKeyPress(press("KeyG", { ctrlKey: true, altKey: true }))).toBe(
      "Control+Alt+G",
    );
  });

  it("orders modifiers canonically so equal combinations compare equal", () => {
    const viaMeta = acceleratorFromKeyPress(
      press("KeyK", { metaKey: true, shiftKey: true, ctrlKey: true }),
    );
    expect(viaMeta).toBe("Control+Shift+Command+K");
  });

  it("maps digits, named keys and function keys", () => {
    expect(acceleratorFromKeyPress(press("Digit1", { metaKey: true }))).toBe("Command+1");
    expect(acceleratorFromKeyPress(press("Space", { altKey: true }))).toBe("Alt+Space");
    expect(acceleratorFromKeyPress(press("ArrowUp", { metaKey: true }))).toBe("Command+Up");
    expect(acceleratorFromKeyPress(press("F5"))).toBe("F5");
  });

  it("returns null for a bare modifier so recording continues", () => {
    expect(acceleratorFromKeyPress(press("ShiftLeft", { shiftKey: true }))).toBeNull();
    expect(acceleratorFromKeyPress(press("MetaLeft", { metaKey: true }))).toBeNull();
  });

  it("returns null for keys with no accelerator spelling", () => {
    expect(acceleratorFromKeyPress(press("CapsLock"))).toBeNull();
    expect(acceleratorFromKeyPress(press("AudioVolumeUp"))).toBeNull();
  });
});

describe("hasEnoughModifiers", () => {
  it("rejects a bare letter that would be stolen from every app", () => {
    expect(hasEnoughModifiers("G")).toBe(false);
    expect(hasEnoughModifiers("1")).toBe(false);
  });

  it("accepts modified combinations and bare function keys", () => {
    expect(hasEnoughModifiers("Control+Alt+G")).toBe(true);
    expect(hasEnoughModifiers("F5")).toBe(true);
  });
});

describe("isFunctionKey", () => {
  it("matches only F1..F24 spellings", () => {
    expect(isFunctionKey("F1")).toBe(true);
    expect(isFunctionKey("F12")).toBe(true);
    expect(isFunctionKey("Command+F1")).toBe(false);
    expect(isFunctionKey("F")).toBe(false);
  });
});

describe("formatAccelerator", () => {
  it("uses macOS menu symbols on mac", () => {
    expect(formatAccelerator("Control+Alt+Shift+Command+G", true)).toBe("⌃⌥⇧⌘G");
    expect(formatAccelerator("Command+Up", true)).toBe("⌘↑");
    expect(formatAccelerator("Alt+Space", true)).toBe("⌥Space");
  });

  it("spells modifiers out elsewhere", () => {
    expect(formatAccelerator("Control+Alt+G", false)).toBe("Control+Alt+G");
    expect(formatAccelerator("Command+Up", false)).toBe("Command+↑");
  });

  it("returns an empty string when nothing is assigned", () => {
    expect(formatAccelerator("", true)).toBe("");
  });
});
