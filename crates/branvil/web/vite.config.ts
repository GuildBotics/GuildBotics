import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The dev server proxies the API to a running `branvil serve`. The service
// checks Host and Origin, so the proxy presents itself as the service origin.
const apiTarget = process.env.BRANVIL_DEV_API ?? "http://127.0.0.1:7878";

const proxy = {
  target: apiTarget,
  changeOrigin: true,
  configure: (server: { on: (event: "proxyReq", cb: (req: { setHeader: (k: string, v: string) => void; getHeader: (k: string) => unknown }) => void) => void }) => {
    server.on("proxyReq", (req) => {
      if (req.getHeader("origin")) req.setHeader("origin", new URL(apiTarget).origin);
    });
  },
};

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // The service sends a CSP without inline scripts or data: URIs.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
  },
  server: {
    proxy: { "/api": proxy, "/git": proxy },
  },
  test: {
    maxWorkers: 4,
    // Native instanceof/toStrictEqual surprises: https://vitest.dev/config/pool#vmthreads
    pool: "vmThreads",
    // A production run alongside the Rust job still reached 4.78s.
    testTimeout: 10_000,
    reporters: ["default", "./test-timing.ts"],
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
