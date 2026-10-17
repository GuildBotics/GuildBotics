import { describe, expect, it, vi } from "vitest";
import type { UserConfig } from "vite";

import viteConfig from "./vite.config";

describe("Vite development server", () => {
  it("does not watch Cargo build outputs", () => {
    const config = viteConfig as UserConfig;

    expect(config.server?.watch?.ignored).toEqual(["**/src-tauri/target/**"]);
  });
});

describe("test mock isolation", () => {
  const persistent = vi.fn(() => "default");
  const oneShot = vi.fn(async () => "default");

  // Each case checks the defaults, then leaves both kinds of override behind.
  // Either case can run first; no component test has to precede another one.
  it.each([1, 2])("restores implementations and unused responses before case %i", async () => {
    expect(persistent).not.toHaveBeenCalled();
    expect(oneShot).not.toHaveBeenCalled();
    expect.soft(persistent()).toBe("default");
    await expect.soft(oneShot()).resolves.toBe("default");

    persistent.mockImplementation(() => "overridden");
    oneShot.mockRejectedValueOnce(new Error("unused response"));
  });
});
