import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    rolldownOptions: {
      // `quick.html` is the hotkey-triggered quick run window.
      input: {
        main: resolve(import.meta.dirname, "index.html"),
        quick: resolve(import.meta.dirname, "quick.html"),
      },
      output: {
        // A single group whose `name` decides per module: returning `null`
        // leaves the module to Rolldown's automatic splitting, and every
        // distinct name it returns becomes its own chunk.
        codeSplitting: { groups: [{ name: vendorChunkName }] },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    // Vitest owns the `src/**` unit/component tests only. The Playwright
    // real-browser journeys under `e2e/**` must never be collected by Vitest.
    exclude: ["node_modules", "dist", ".idea", ".git", ".cache", "e2e/**"],
    // A component test that drives several Mantine Selects and types into a
    // field through userEvent costs 1.5-2.8s on a developer machine, and the
    // suite runs 35 files in parallel on a 4-core CI runner. Vitest's 5s
    // default leaves no headroom for that difference, so whichever of those
    // tests loses the race fails while the rest pass. This is the budget for a
    // hung test, not a target: a test that needs seconds of real waiting is
    // still a test to fix.
    testTimeout: 20000,
  },
});

function vendorChunkName(id: string): string | null {
  if (!id.includes("node_modules")) {
    return null;
  }
  const packageName = getPackageName(id);
  if (!packageName) {
    return "vendor";
  }
  if (packageName.startsWith("@mantine/")) {
    return "vendor-mantine";
  }
  if (["react", "react-dom", "scheduler"].includes(packageName)) {
    return "vendor-react";
  }
  if (packageName.startsWith("@tanstack/")) {
    return "vendor-query";
  }
  if (packageName === "i18next" || packageName === "react-i18next") {
    return "vendor-i18n";
  }
  if (packageName === "lucide-react") {
    return "vendor-icons";
  }
  if (packageName.startsWith("@tauri-apps/")) {
    return "vendor-tauri";
  }
  return "vendor";
}

function getPackageName(id: string): string | null {
  const nodeModulesPath = id.split("node_modules/").pop();
  if (!nodeModulesPath) {
    return null;
  }
  const [scopeOrName, name] = nodeModulesPath.split("/");
  return scopeOrName.startsWith("@") && name ? `${scopeOrName}/${name}` : scopeOrName;
}
