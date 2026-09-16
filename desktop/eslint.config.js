import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: [
      "dist",
      "node_modules",
      "src-tauri",
      "coverage",
      "eslint.config.js",
      "vite.config.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
    },
  },
  {
    files: ["src/tracePresentation.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "./api/client",
              importNames: ["TraceRecord"],
              message:
                "Diagnostics presentation must consume TracePresentation, not raw TraceRecord payloads.",
            },
          ],
        },
      ],
    },
  },
  {
    // Component tests render under `src/test/TestMantineProvider`, which is the
    // one place that decides what Mantine does under test -- above all, that
    // transitions carry no timers. A test that reaches for Mantine's own
    // provider brings those timers back, and the suite then fails in whichever
    // file happens to be running when one of them fires.
    files: ["src/**/*.test.tsx"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@mantine/core",
              importNames: ["MantineProvider"],
              message:
                "Render component tests under TestMantineProvider from src/test/TestMantineProvider.",
            },
          ],
        },
      ],
    },
  },
  {
    // The Playwright E2E harness runs in Node (launcher script + specs that touch
    // the filesystem), so give those files Node globals instead of browser ones.
    files: ["e2e/**/*.{ts,mjs}", "playwright.config.ts"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.node, fetch: "readonly" },
    },
  },
);
