import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

// Journey ⑤: Diagnostics against the REAL backend.
//
// The "diagnostics" stack is seeded like the configured one: a project with a
// fake OpenAI key (`sk-e2e-test-key`) and CLI agent `codex`, GitHub/Slack
// disabled. This journey:
//   * asserts the readiness tab renders backend-derived status badges (config
//     Ready, env Found, GitHub Disabled) for the seeded workspace;
//   * runs the real scenario diagnostics (`POST /diagnostics/scenario`) and
//     asserts the LLM check FAILS — the fake key cannot satisfy a live LLM call,
//     so the backend deterministically returns an error check that surfaces as a
//     red "LLM check failed" alert.

const here = dirname(fileURLToPath(import.meta.url));

type StackContext = {
  seeded: boolean;
  memberId: string | null;
};

function readDiagnosticsContext(): StackContext {
  const raw = readFileSync(join(here, ".stack-context-diagnostics.json"), "utf-8");
  return JSON.parse(raw) as StackContext;
}

test("renders readiness badges and reports the real LLM failure from scenario diagnostics", async ({
  page,
}) => {
  const ctx = readDiagnosticsContext();
  expect(ctx.seeded).toBe(true);

  await page.goto("/#/diagnostics");
  await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();

  // Readiness badges are derived from the real /config/status, /team and
  // /config/project responses for the seeded workspace: config "Configured",
  // env file "Detected", GitHub "Disabled".
  await expect(page.getByText("Configured", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("Detected", { exact: true })).toBeVisible();
  await expect(page.getByText("Disabled", { exact: true })).toBeVisible();

  // Run the real read-only scenario diagnostics.
  const runButton = page.getByRole("button", { name: "Validate settings" });
  await expect(runButton).toBeEnabled();
  await runButton.click();

  // The seeded fake OpenAI key cannot complete a live LLM call, so the backend
  // returns an error check that the UI renders as a red "LLM check failed" alert.
  // Use a generous timeout: the real provider call must round-trip and fail.
  await expect(page.getByText("LLM check failed")).toBeVisible({ timeout: 45_000 });
  await expect(
    page.getByText("The selected LLM provider did not accept the minimal validation request."),
  ).toBeVisible();

  // The all-ok summary must NOT appear when a check failed.
  await expect(page.getByText("Settings validated")).toHaveCount(0);
});
