import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) {
            return undefined;
          }
          const packageName = getPackageName(id);
          if (!packageName) {
            return "vendor";
          }
          if (packageName.startsWith("@mantine/") || packageName === "mantine-form-zod-resolver") {
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
        },
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
  },
});

function getPackageName(id: string): string | null {
  const nodeModulesPath = id.split("node_modules/").pop();
  if (!nodeModulesPath) {
    return null;
  }
  const [scopeOrName, name] = nodeModulesPath.split("/");
  return scopeOrName.startsWith("@") && name ? `${scopeOrName}/${name}` : scopeOrName;
}
