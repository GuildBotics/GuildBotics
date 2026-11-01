import { afterEach, describe, expect, it, vi } from "vitest";

import { openExternal } from "./openExternal";

describe("openExternal", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens a new tab, with no reference back to GuildBotics", async () => {
    const windowOpen = vi.spyOn(window, "open").mockReturnValue(null);

    await openExternal("https://github.com/acme/demo/issues/1");

    expect(windowOpen).toHaveBeenCalledWith(
      "https://github.com/acme/demo/issues/1",
      "_blank",
      "noopener,noreferrer",
    );
  });
});
