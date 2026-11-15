import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n";
import { applyChange } from "../query/events";
import { createQueryClient } from "../query/hooks";
import { apiError, fakeApi, json } from "../test/fakeApi";
import { theme } from "@guildbotics/ui/theme";
import { TipChecks } from "../views/pulls/TipChecks";
import type { Verification, VerificationState } from "../api/types";

const SHA = "a".repeat(40);
const OLD = "b".repeat(40);
const state = (name: VerificationState = "verified"): Verification => ({
  branch: "main", tip: name === "empty" ? null : SHA, state: name, ahead: name === "verified" ? 0 : 3,
  latest_verified: { sha: OLD, verified_at: "2026-11-15" }, failing_since: null,
  current: { sha: OLD, base: null, state: "running", started_at: "2026-11-15", finished_at: null, retry: { number: 1, max: 3 }, failed_jobs: [] },
});
function show(paths: string[], repo: string | null = null, language: "en" | "ja" = "en") {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: Infinity } });
  render(<MantineProvider theme={theme} env="test"><QueryClientProvider client={client}><I18nProvider initial={language}>
    <TipChecks workspace="acme" paths={paths} repo={repo} />
  </I18nProvider></QueryClientProvider></MantineProvider>);
  return client;
}
const history = { items: [{ sha: SHA, title: "new", author: "Test", authored_at: "2026-11-15", mark: "verifying" }, { sha: OLD, title: "old", author: "Test", authored_at: "2026-11-14", mark: "verified" }], next_cursor: "next" };

describe("default branch verification", () => {
  it.each([ ["verified", "確認済み", "success"], ["verifying", "確認中（再試行 1/3）", "warning"], ["pending", "確認待ち", "warning"], ["failing", "失敗", "danger"], ["empty", "コミットなし", "color-bg-hover"] ] as const)("shows %s with its tone", async (name, label, color) => {
    fakeApi({ "GET /api/v1/repos/acme/demo/verification": json(200, state(name)) });
    show(["acme/demo"], "acme/demo", "ja");
    const toggle = screen.getByRole("button");
    await waitFor(() => expect(toggle).toHaveTextContent(label));
    expect(toggle.querySelector(".mantine-Badge-root")).toHaveAttribute("data-variant", "light");
    expect(toggle.querySelector(".mantine-Badge-root")?.getAttribute("style")).toContain(color);
  });

  it("shows the verification commit checks, distance, marks and cursor pages and refreshes both queries", async () => {
    let value = state("verifying");
    const fake = fakeApi({
      "GET /api/v1/repos/acme/demo/verification": () => json(200, value),
      "GET /api/v1/repos/acme/demo/verification/commits": (request) => json(200, new URLSearchParams(request.search).get("cursor") ? { items: [{ ...history.items[0], sha: "c".repeat(40), title: "failed commit", mark: "failed" }], next_cursor: null } : history),
      [`GET /api/v1/repos/acme/demo/commits/${OLD}/checks`]: json(200, { sha: OLD, checks: [], checks_expected: false }),
    });
    const client = show(["acme/demo"], "acme/demo", "ja");
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent("確認中（再試行 1/3）"));
    await user.click(screen.getByRole("button"));
    expect(await screen.findByText("確認済みから 3 本進んでいる")).toBeInTheDocument();
    expect(await screen.findByText("new")).toBeInTheDocument();
    expect(screen.getByText("確認済み")).toBeInTheDocument();
    expect(screen.getByText("確認中")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "続きを読む" }));
    expect(await screen.findByText("failed commit")).toBeInTheDocument();
    expect(screen.getByText("失敗")).toBeInTheDocument();
    value = { ...state("pending"), latest_verified: null };
    act(() => applyChange({ kind: "verification", path: "acme/demo" }, client));
    expect(await screen.findByText("確認済みのコミットはまだない")).toBeInTheDocument();
    await waitFor(() => expect(fake.calls("GET", "/api/v1/repos/acme/demo/verification/commits").length).toBeGreaterThan(2));
    expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${SHA}/checks`)).toHaveLength(0);
  });

  it("counts repository states while closed and reports refresh errors", async () => {
    const fake = fakeApi({
      "GET /api/v1/repos/acme/a/verification": [json(200, state()), apiError(403, "forbidden", "Denied")],
      "GET /api/v1/repos/acme/b/verification": json(200, state("pending")),
    });
    const client = show(["acme/a", "acme/b"]);
    const toggle = screen.getByRole("button");
    await waitFor(() => expect(toggle).toHaveTextContent("verified 1"));
    expect(toggle).toHaveTextContent("awaiting verification 1");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(fake.requests).toHaveLength(2);
    act(() => applyChange({ kind: "verification", path: "acme/a" }, client));
    await waitFor(() => expect(toggle).toHaveTextContent("could not load 1"));
    expect(toggle).not.toHaveTextContent("verified 1");
  });
});
