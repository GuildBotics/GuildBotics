import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { readStackContext } from "./stack-context";

// Journey ⑤: Diagnostics against the REAL backend.
//
// The "diagnostics" stack is seeded WITHOUT an LLM API key (offline mode), so
// the backend's missing-key short-circuit in `_check_llm` returns a fast error
// WITHOUT firing a live LLM call. This keeps `npm run e2e` fully offline and
// deterministic — no dependency on api.openai.com latency or availability —
// while still exercising the real backend-frontend wiring end-to-end. This
// journey:
//   * asserts the Settings screen's Verification section renders backend-derived
//     status badges (config Configured, env Detected, GitHub Disabled) for the
//     seeded workspace;
//   * runs the real scenario diagnostics (`POST /diagnostics/scenario`) and
//     asserts the missing-key check renders the i18n-mapped "LLM API key is
//     missing" alert, plus a context line naming the env var (OPENAI_API_KEY);
//   * reads transcript settings/storage from the real backend and opens the
//     pinned Global/system transcript.

test("renders readiness badges and reports the missing-key LLM check from scenario diagnostics", async ({
  page,
}) => {
  const ctx = readStackContext("diagnostics");
  expect(ctx.seeded).toBe(true);
  expect(ctx.seededWithoutLlmKey).toBe(true);

  // Readiness badges and the scenario check live in the Verification section of
  // the Settings screen; the Diagnostics screen owns executions and transcripts.
  await page.goto("/#/setup?section=verification");
  await expect(page.getByRole("heading", { name: "Verification" }).first()).toBeVisible();

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

  await page.goto("/#/diagnostics");
  await expect(page.getByRole("heading", { name: "Diagnostics" })).toBeVisible();

  await page.getByRole("tab", { name: "Settings" }).click();
  await expect(page.getByText("Session transcripts", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Detail" })).toHaveValue("Standard (recommended)");
  await expect(page.getByText("rebuild threshold: 8.0 MiB", { exact: false })).toBeVisible();
  await expect(page.getByText("0 B / 8.0 MiB", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Retention days" })).toHaveValue("30");

  await page.getByRole("tab", { name: "Executions" }).click();
  await expect(page.getByText("Global / system", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Service events & unscoped logs")).toBeVisible();
});

test("sends a troubleshooting question through the real backend and reports the failure", async ({
  page,
}) => {
  // `functions/troubleshoot` is a `brain: agent` command, so the assistant turn
  // reaches the real FastAPI endpoint and launches the member's AI CLI tool —
  // which the harness has shadowed with a stub that fails immediately. That is
  // exactly the wire this journey exists to prove: client.ts ->
  // /diagnostics/troubleshoot -> AppRuntime -> error mapping -> the panel's
  // error alert. Answer quality and the branch matrix are covered by the unit /
  // component tests.
  const ctx = readStackContext("diagnostics");

  await page.goto("/#/diagnostics?tab=executions");
  await page.getByRole("button", { name: "Ask AI" }).click();

  const panel = page.getByRole("region", { name: "Troubleshooting AI" });
  await expect(panel).toBeVisible();
  // The executions list stays visible: the drawer overlays, it does not replace.
  await expect(page.getByText("Global / system", { exact: true }).first()).toBeVisible();

  const request = page.waitForResponse(
    (response) =>
      response.url().includes("/diagnostics/troubleshoot") &&
      response.request().method() === "POST",
  );
  await panel.getByLabel("Message to troubleshooting AI").fill("Why did the service fail?");
  await panel.getByRole("button", { name: "Send" }).click();

  // The wire contract this journey proves: the stub's non-zero exit comes back
  // as the typed 502 (not an unmapped 500, not a CORS-blocked "Failed to
  // fetch"), and the panel shows the backend's reason for the failure, not
  // just a generic failure title. The exact wording is owned by the CLI agent
  // adapter, so the assertion pins the error type and echoes the response
  // message into the panel instead of hard-coding adapter prose.
  const response = await request;
  expect(response.status()).toBe(502);
  const body = await response.json();
  expect(body.code).toBe("troubleshooting_failed");
  expect(body.message).toContain("exited with code 1");
  await expect(panel.getByText("The troubleshooting AI could not answer")).toBeVisible();
  await expect(panel.getByText(body.message)).toBeVisible();

  // The turn stopped at the stub: the failure came from the harness, not from a
  // real — possibly logged-in — binary on the machine running the suite.
  expect(readFileSync(ctx.cliStubLog, "utf-8")).toMatch(/^codex\b/m);
});
