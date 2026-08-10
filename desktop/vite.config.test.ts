import { describe, expect, it } from "vitest";
import type { UserConfig } from "vite";

import viteConfig from "./vite.config";

describe("Vite development server", () => {
  it("does not watch Cargo build outputs", () => {
    const config = viteConfig as UserConfig;

    expect(config.server?.watch?.ignored).toEqual(["**/src-tauri/target/**"]);
  });
});
