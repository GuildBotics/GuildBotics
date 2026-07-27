import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import i18n, {
  followAppLanguage,
  getInitialAppLanguage,
  normalizeLanguage,
  setAppLanguage,
} from "./i18n";

const emit = vi.fn(async () => undefined);
let listener: ((event: { payload: string }) => void) | undefined;
const unlisten = vi.fn();
vi.mock("@tauri-apps/api/event", () => ({
  emit: (...args: unknown[]) => emit(...(args as [])),
  listen: async (_event: string, handler: (event: { payload: string }) => void) => {
    listener = handler;
    return unlisten;
  },
}));

describe("normalizeLanguage", () => {
  it("accepts exact supported language codes", () => {
    expect(normalizeLanguage("en")).toBe("en");
    expect(normalizeLanguage("ja")).toBe("ja");
  });

  it("normalizes locale strings to supported language codes", () => {
    expect(normalizeLanguage("en-US")).toBe("en");
    expect(normalizeLanguage("ja-JP")).toBe("ja");
  });

  it("returns null for unsupported or missing values", () => {
    expect(normalizeLanguage("fr-FR")).toBeNull();
    expect(normalizeLanguage("")).toBeNull();
    expect(normalizeLanguage(undefined)).toBeNull();
  });
});

describe("getInitialAppLanguage", () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(window.navigator, "language");

  function mockNavigatorLanguage(value: string) {
    Object.defineProperty(window.navigator, "language", {
      configurable: true,
      get: () => value,
    });
  }

  beforeEach(() => {
    localStorage.clear();
    mockNavigatorLanguage("en-US");
  });

  afterEach(() => {
    if (originalDescriptor) {
      Object.defineProperty(window.navigator, "language", originalDescriptor);
    }
    localStorage.clear();
  });

  it("uses a valid stored language", () => {
    localStorage.setItem("guildbotics.appLanguage", "ja");
    expect(getInitialAppLanguage()).toBe("ja");
  });

  it("falls back to navigator language when storage holds an invalid value", () => {
    localStorage.setItem("guildbotics.appLanguage", "fr");
    mockNavigatorLanguage("ja-JP");
    expect(getInitialAppLanguage()).toBe("ja");
  });

  it("falls back to en when both storage and navigator are unsupported", () => {
    localStorage.setItem("guildbotics.appLanguage", "fr");
    mockNavigatorLanguage("fr-FR");
    expect(getInitialAppLanguage()).toBe("en");
  });

  it("uses navigator language when storage is empty", () => {
    mockNavigatorLanguage("ja");
    expect(getInitialAppLanguage()).toBe("ja");
  });
});

describe("app language storage", () => {
  afterEach(async () => {
    localStorage.clear();
    await setAppLanguage("en");
  });

  it("persists the selected application language", async () => {
    localStorage.clear();

    await setAppLanguage("ja");

    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("ja");
    expect(getInitialAppLanguage()).toBe("ja");
    expect(i18n.language).toBe("ja");
  });

  it("updates both localStorage and i18next state when switching back", async () => {
    await setAppLanguage("ja");
    await setAppLanguage("en");

    expect(localStorage.getItem("guildbotics.appLanguage")).toBe("en");
    expect(i18n.language).toBe("en");
  });
});

describe("i18n resources", () => {
  function collectKeys(node: unknown, prefix = ""): string[] {
    if (node && typeof node === "object" && !Array.isArray(node)) {
      return Object.entries(node as Record<string, unknown>).flatMap(([key, value]) =>
        collectKeys(value, prefix ? `${prefix}.${key}` : key),
      );
    }
    return [prefix];
  }

  it("has identical translation keys between en and ja", () => {
    const en = i18n.getResourceBundle("en", "translation") as Record<string, unknown>;
    const ja = i18n.getResourceBundle("ja", "translation") as Record<string, unknown>;

    const enKeys = collectKeys(en).sort();
    const jaKeys = collectKeys(ja).sort();

    const missingInJa = enKeys.filter((key) => !jaKeys.includes(key));
    const missingInEn = jaKeys.filter((key) => !enKeys.includes(key));

    expect(missingInJa).toEqual([]);
    expect(missingInEn).toEqual([]);
  });

  it("gives every assistant chat namespace the same leaf keys", () => {
    // AssistantChatPanel resolves its labels as `${namespace}.${leaf}`, so a
    // namespace missing a leaf would render a raw key at runtime.
    const bundle = i18n.getResourceBundle("en", "translation") as Record<
      string,
      Record<string, unknown>
    >;
    const authoring = collectKeys(bundle.commands.authoring).sort();
    const troubleshooting = collectKeys(bundle.diagnostics.troubleshooting).sort();

    expect(authoring).not.toEqual([]);
    expect(authoring.filter((key) => !troubleshooting.includes(key))).toEqual([]);
  });
});

describe("language changes across windows", () => {
  // The quick run window keeps its own webview and never reloads, so a change
  // made in the main window has to reach it as an event.
  beforeEach(() => {
    emit.mockClear();
    unlisten.mockClear();
    listener = undefined;
    Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  });

  afterEach(async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await i18n.changeLanguage("en");
  });

  it("announces the new language to the other windows", async () => {
    await setAppLanguage("ja");

    expect(emit).toHaveBeenCalledWith("app://language-changed", "ja");
  });

  it("adopts a language announced by another window", async () => {
    const stop = followAppLanguage();
    await vi.waitFor(() => expect(listener).toBeDefined());

    listener!({ payload: "ja" });

    await vi.waitFor(() => expect(i18n.language).toBe("ja"));
    stop();
    expect(unlisten).toHaveBeenCalled();
  });

  it("stays quiet outside the desktop shell", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;

    await setAppLanguage("ja");

    expect(emit).not.toHaveBeenCalled();
    expect(followAppLanguage()).toBeTypeOf("function");
  });
});
