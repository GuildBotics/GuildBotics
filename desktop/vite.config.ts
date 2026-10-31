import type { ClientRequest } from "node:http";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, type HttpProxy } from "vite";

// The backend `vite dev` stands in front of: the pages come from Vite (with
// HMR) and every `/api` request, the event stream included, from the backend.
const BACKEND = `http://127.0.0.1:${process.env.GUILDBOTICS_SERVER_PORT ?? 8765}`;

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    rolldownOptions: {
      // `quick.html` is the hotkey-triggered quick run window; `bootstrap.html`
      // is the bundled page a Desktop window shows until the backend is up.
      input: {
        main: resolve(import.meta.dirname, "index.html"),
        quick: resolve(import.meta.dirname, "quick.html"),
        bootstrap: resolve(import.meta.dirname, "bootstrap.html"),
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
    watch: {
      ignored: ["**/src-tauri/target/**"],
    },
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true, ws: true, configure: asBackendOrigin },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    // Reset implementations and unused one-shot responses before every test.
    mockReset: true,
    // CI runs the suite beside other jobs on the same CPUs. Longer than the
    // findBy* wait in src/test/setup.ts, so a slow render fails naming the
    // element it waited for rather than as a bare timeout.
    testTimeout: 20_000,
    // Vitest owns the `src/**` unit/component tests only. The Playwright
    // real-browser journeys under `e2e/**` must never be collected by Vitest.
    exclude: ["node_modules", "dist", ".idea", ".git", ".cache", "e2e/**"],
  },
});

/**
 * The backend accepts a request carrying `Origin` only from its own origin, and
 * the pages here come from the dev server's: the proxy speaks as the backend.
 */
function asBackendOrigin(proxy: HttpProxy.ProxyServer) {
  const rewrite = (request: ClientRequest) => {
    if (request.getHeader("origin")) {
      request.setHeader("origin", BACKEND);
    }
  };
  proxy.on("proxyReq", rewrite);
  proxy.on("proxyReqWs", rewrite);
}

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
