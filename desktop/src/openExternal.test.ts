import { afterEach, describe, expect, it, vi } from "vitest";

import { openExternal } from "./openExternal";

const shellOpen = vi.hoisted(() => vi.fn(async () => undefined));
vi.mock("@tauri-apps/plugin-shell", () => ({ open: shellOpen }));

describe("openExternal", () => {
  afterEach(() => {
    delete (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
    shellOpen.mockClear();
    vi.restoreAllMocks();
  });

  it("opens a new tab in the browser, with no reference back to GuildBotics", async () => {
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);

    await openExternal("https://github.com/acme/demo/issues/1");

    expect(windowOpen).toHaveBeenCalledWith(
      "https://github.com/acme/demo/issues/1",
      "_blank",
      "noopener,noreferrer",
    );
    expect(shellOpen).not.toHaveBeenCalled();
  });

  it("hands the page to the system browser from a Desktop window", async () => {
    (window as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ = {};
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);

    await openExternal("https://github.com/acme/demo/issues/1");

    expect(shellOpen).toHaveBeenCalledWith("https://github.com/acme/demo/issues/1");
    expect(windowOpen).not.toHaveBeenCalled();
  });
});
