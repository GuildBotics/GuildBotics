// Translation between a physical key press and the accelerator string the
// Tauri global-shortcut plugin registers.
//
// Recording uses `KeyboardEvent.code` rather than `key`: on macOS, Option
// rewrites `key` into the composed character (Option+G becomes "©"), while
// `code` keeps naming the physical key the user actually pressed.

/** Modifier order used when building an accelerator, so equal combos compare equal. */
const MODIFIER_ORDER = ["Control", "Alt", "Shift", "Command"] as const;

const NAMED_CODES: Record<string, string> = {
  Space: "Space",
  Enter: "Enter",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  Escape: "Escape",
  Home: "Home",
  End: "End",
  PageUp: "PageUp",
  PageDown: "PageDown",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
  Minus: "Minus",
  Equal: "Equal",
  BracketLeft: "BracketLeft",
  BracketRight: "BracketRight",
  Backslash: "Backslash",
  Semicolon: "Semicolon",
  Quote: "Quote",
  Comma: "Comma",
  Period: "Period",
  Slash: "Slash",
  Backquote: "Backquote",
};

const DISPLAY_SYMBOLS: Record<string, string> = {
  Control: "⌃",
  Alt: "⌥",
  Shift: "⇧",
  Command: "⌘",
};

const DISPLAY_KEYS: Record<string, string> = {
  Space: "Space",
  Enter: "↩",
  Tab: "⇥",
  Backspace: "⌫",
  Delete: "⌦",
  Escape: "⎋",
  Up: "↑",
  Down: "↓",
  Left: "←",
  Right: "→",
  Minus: "-",
  Equal: "=",
  BracketLeft: "[",
  BracketRight: "]",
  Backslash: "\\",
  Semicolon: ";",
  Quote: "'",
  Comma: ",",
  Period: ".",
  Slash: "/",
  Backquote: "`",
};

export type KeyPress = {
  code: string;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
};

function baseKeyFromCode(code: string): string | null {
  const letter = /^Key([A-Z])$/.exec(code);
  if (letter) {
    return letter[1];
  }
  const digit = /^Digit(\d)$/.exec(code);
  if (digit) {
    return digit[1];
  }
  const functionKey = /^F(\d{1,2})$/.exec(code);
  if (functionKey) {
    return code;
  }
  return NAMED_CODES[code] ?? null;
}

/**
 * Build an accelerator from a key press.
 *
 * Returns null while the press cannot stand on its own yet — a bare modifier,
 * or a key with no accelerator spelling — so the caller keeps recording instead
 * of committing a half-finished combination.
 */
export function acceleratorFromKeyPress(press: KeyPress): string | null {
  const base = baseKeyFromCode(press.code);
  if (!base) {
    return null;
  }

  const held = new Set<string>();
  if (press.ctrlKey) {
    held.add("Control");
  }
  if (press.altKey) {
    held.add("Alt");
  }
  if (press.shiftKey) {
    held.add("Shift");
  }
  if (press.metaKey) {
    held.add("Command");
  }

  const modifiers = MODIFIER_ORDER.filter((modifier) => held.has(modifier));
  return [...modifiers, base].join("+");
}

/** Function keys are the only combinations usable without a modifier. */
export function isFunctionKey(accelerator: string): boolean {
  return /^F\d{1,2}$/.test(accelerator);
}

/**
 * A global shortcut takes its combination away from every other app, so a
 * bare letter or digit is never acceptable.
 */
export function hasEnoughModifiers(accelerator: string): boolean {
  return accelerator.includes("+") || isFunctionKey(accelerator);
}

/** Render an accelerator the way the platform writes it on menus. */
export function formatAccelerator(accelerator: string, isMac: boolean): string {
  if (!accelerator) {
    return "";
  }
  const parts = accelerator.split("+");
  const base = parts[parts.length - 1];
  const modifiers = parts.slice(0, -1);
  const key = DISPLAY_KEYS[base] ?? base;

  if (isMac) {
    return `${modifiers.map((modifier) => DISPLAY_SYMBOLS[modifier] ?? modifier).join("")}${key}`;
  }
  return [...modifiers, key].join("+");
}

export function isMacPlatform(): boolean {
  return typeof navigator !== "undefined" && /Mac/i.test(navigator.platform || navigator.userAgent);
}
