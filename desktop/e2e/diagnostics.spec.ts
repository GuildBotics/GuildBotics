import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ⑤: Diagnostics against the REAL backend.
//
// The "diagnostics" stack is seeded WITHOUT an LLM API key (offline mode), so
// the backend's missing-key short-circuit in `_check_llm` returns a fast error
// WITHOUT firing a live LLM call. This keeps `npm run e2e` fully offline and
// deterministic — no dependency on api.openai.com latency or availability —
// while still exercising the real backend-frontend wiring end-to-end. This
// journey:
//   * asserts the readiness tab renders backend-derived status badges (config
//     Configured, env Detected, GitHub Disabled) for the seeded workspace;
//   * runs the real scenario diagnostics (`POST /diagnostics/scenario`) and
//     asserts the missing-key check renders the i18n-mapped "LLM API key is
//     missing" alert, plus a context line naming the env var (OPENAI_API_KEY);
//   * reads transcript settings/storage from the real backend and opens the
//     pinned Global/system transcript.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  seeded: boolean;
  seededWithoutLlmKey: boolean;
  memberId: string | null;
};

function readDiagnosticsContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-diagnostics.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("renders readiness badges and reports the missing-key LLM check from scenario diagnostics", async ({
  page,
}) => {
  const ctx = readDiagnosticsContext();
  expect(ctx.seeded).toBe(true);
  expect(ctx.seededWithoutLlmKey).toBe(true);

  await page.goto("/#/diagnostics");
  await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();

  // Readiness badges are derived from the real /config/status, /team and
  // /config/project responses for the seeded workspace: config "Configured",
  // env file "Detected", GitHub "Disabled".
  await expect(page.getByText("Configured", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Detected", { exact: true })).toBeVisible();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
  // Run the real read-only scenario diagnostics. With no OpenAI key configured
  // the backend short-circuits BEFORE any network call, so this resolves in
  // milliseconds rather than waiting for a live HTTPS round-trip.
  const runButton = page.getByRole("button", { name: "Validate settings" });
  await expect(runButton).toBeEnabled();
  await runButton.click();

  // The missing-key check uses the existing `llm_api_key` i18n entry shared
  // with the static verify checks: title "LLM API key is missing" + the
  // env-var detail "OPENAI_API_KEY is not configured" surfaced from the
  // backend's check message.
  await expect(page.getByText("LLM API key is missing")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/OPENAI_API_KEY is not configured/)).toBeVisible();

  // The all-ok summary must NOT appear when a check failed.
  await expect(page.getByText("Settings validated")).toHaveCount(0);

  await page.getByRole("tab", { name: "Diagnostics settings" }).click();
  await expect(page.getByText("Session transcripts", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Detail" })).toHaveValue(
    "Standard (recommended)",
  );
  await expect(page.getByText("rebuild threshold: 8.0 MiB", { exact: false })).toBeVisible();
  await expect(page.getByText("0 B / 8.0 MiB", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Retention days" })).toHaveValue("30");

  await page.getByRole("tab", { name: "Executions" }).click();
  await expect(page.getByText("Global / system", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Service events & unscoped logs")).toBeVisible();
});
