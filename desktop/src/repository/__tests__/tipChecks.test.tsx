import { TestMantineProvider } from "../../test/TestMantineProvider";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n";
import { applyChange } from "../query/events";
import { createQueryClient } from "../query/hooks";
import { apiError, fakeApi, json } from "../test/fakeApi";
import { theme } from "../../ui/theme";
import { TipChecks } from "../views/pulls/TipChecks";
import type { Verification, VerificationState } from "../api/types";

const SHA = "a".repeat(40);
const OLD = "b".repeat(40);
const state = (name: VerificationState = "verified"): Verification => ({
  repair: null,
  branch: "main",
  tip: name === "empty" ? null : SHA,
  state: name,
  ahead: name === "verified" ? 0 : 3,
  latest_verified: { sha: OLD, verified_at: "2026-11-15" },
  failing_since: null,
  current: {
    sha: OLD,
    base: null,
    state: "running",
    started_at: "2026-11-15",
    finished_at: null,
    retry: { number: 1, max: 3 },
    failed_jobs: [],
  },
});
function show(paths: string[], repo: string | null = null, language: "en" | "ja" = "en") {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: Infinity } });
  render(
    <TestMantineProvider theme={theme}>
      <QueryClientProvider client={client}>
        <I18nProvider initial={language}>
          <TipChecks workspace="acme" paths={paths} repo={repo} />
        </I18nProvider>
      </QueryClientProvider>
    </TestMantineProvider>,
  );
  return client;
}
const history = {
  items: [
    {
      sha: SHA,
      title: "new",
      author: "Test",
      authored_at: "2026-11-15T11:02:59+09:00",
      mark: "verifying",
    },
    {
      sha: OLD,
      title: "old",
      author: "Test",
      authored_at: "2026-11-14T22:15:00Z",
      mark: "verified",
    },
  ],
  next_cursor: "next",
};

describe("default branch verification", () => {
  it.each(["ja", "en"] as const)(
    "links the open repair in header and drawer (%s)",
    async (language) => {
      fakeApi({
        "GET /api/v1/repos/acme/demo/verification": json(200, {
          ...state("failing"),
          repair: { number: 12, assignee: "alice", url: "/workspaces/acme/repos/demo/tickets/12" },
        }),
        "GET /api/v1/repos/acme/demo/verification/commits": json(200, {
          items: [],
          next_cursor: null,
        }),
        [`GET /api/v1/repos/acme/demo/commits/${OLD}/checks`]: json(200, {
          sha: OLD,
          checks: [],
          checks_expected: false,
        }),
      });
      show(["acme/demo"], "acme/demo", language);
      const text =
        language === "ja" ? "失敗（修復 #12 を待っている）" : "failed (waiting for repair #12)";
      await waitFor(() => expect(screen.getByRole("button")).toHaveTextContent(text));
      expect(screen.getByRole("link", { name: "#12" })).toHaveAttribute(
        "href",
        "/workspaces/acme/repos/demo/tickets/12",
      );
      await userEvent.setup().click(screen.getByRole("button"));
      const dialog = within(await screen.findByRole("dialog"));
      expect(dialog.getByText(text)).toBeInTheDocument();
      expect(dialog.getByRole("link", { name: "#12" })).toHaveAttribute(
        "href",
        "/workspaces/acme/repos/demo/tickets/12",
      );
    },
  );

  it.each([
    ["verified", "確認済み", "success"],
    ["verifying", "確認中（再試行 1/3）", "warning"],
    ["pending", "確認待ち", "warning"],
    ["failing", "失敗", "danger"],
    ["empty", "コミットなし", "color-bg-hover"],
  ] as const)("shows %s with its tone", async (name, label, color) => {
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
      "GET /api/v1/repos/acme/demo/verification/commits": (request) =>
        json(
          200,
          new URLSearchParams(request.search).get("cursor")
            ? {
                items: [
                  {
                    ...history.items[0],
                    sha: "c".repeat(40),
                    title: "failed commit",
                    mark: "failed",
                  },
                ],
                next_cursor: null,
              }
            : history,
        ),
      [`GET /api/v1/repos/acme/demo/commits/${OLD}/checks`]: json(200, {
        sha: OLD,
        checks: [],
        checks_expected: false,
      }),
    });
    const client = show(["acme/demo"], "acme/demo", "ja");
    const user = userEvent.setup();
    await waitFor(() =>
      expect(screen.getByRole("button")).toHaveTextContent("確認中（再試行 1/3）"),
    );
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
    await waitFor(() =>
      expect(
        fake.calls("GET", "/api/v1/repos/acme/demo/verification/commits").length,
      ).toBeGreaterThan(2),
    );
    expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${SHA}/checks`)).toHaveLength(0);
  });

  it("links the failed attempt after a retry passed on a different runner", async () => {
    fakeApi({
      "GET /api/v1/repos/acme/demo/verification": json(200, {
        ...state(),
        runner_recoveries: [
          {
            name: "rust-test",
            failed_attempt: "att_failed",
            failed_runner: "windows",
            successful_runner: "linux",
          },
        ],
      }),
      "GET /api/v1/repos/acme/demo/verification/commits": json(200, {
        items: [],
        next_cursor: null,
      }),
    });
    show(["acme/demo"], "acme/demo");
    await userEvent.setup().click(screen.getByRole("button"));
    const dialog = within(await screen.findByRole("dialog"));
    expect(
      await dialog.findByRole("link", { name: "Failed on windows, passed on linux" }),
    ).toHaveAttribute("href", "/workspaces/acme/repos/demo/jobs/att_failed");
  });

  it("counts repository states while closed and reports refresh errors", async () => {
    const fake = fakeApi({
      "GET /api/v1/repos/acme/a/verification": [
        json(200, state()),
        apiError(403, "forbidden", "Denied"),
      ],
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
  it.each(["en", "ja"] as const)("formats every history timestamp in %s", async (language) => {
    fakeApi({
      "GET /api/v1/repos/acme/demo/verification": json(200, state()),
      "GET /api/v1/repos/acme/demo/verification/commits": json(200, {
        ...history,
        next_cursor: null,
      }),
    });
    show(["acme/demo"], "acme/demo", language);
    await userEvent.setup().click(screen.getByRole("button"));
    await screen.findByText("new");
    for (const commit of history.items) {
      const time = screen.getByTitle(commit.authored_at);
      expect(time.tagName).toBe("TIME");
      expect(time).toHaveAttribute("datetime", commit.authored_at);
      expect(time).toHaveTextContent(new Date(commit.authored_at).toLocaleString(language));
      expect(time).not.toHaveTextContent(commit.authored_at);
    }
  });

  it.each(["verified", "verifying", "pending", "failing", "empty"] as const)(
    "shows one %s badge per repository and no redundant empty explanation",
    async (name) => {
      const value = { ...state(name), latest_verified: null };
      fakeApi({
        "GET /api/v1/repos/acme/demo/verification": json(200, value),
        "GET /api/v1/repos/acme/demo/verification/commits": json(200, {
          items: [],
          next_cursor: null,
        }),
        [`GET /api/v1/repos/acme/demo/commits/${OLD}/checks`]: json(200, {
          sha: OLD,
          checks: [],
          checks_expected: false,
        }),
      });
      show(["acme/demo"], null, "ja");
      await waitFor(() => expect(screen.getByRole("button")).not.toHaveTextContent("読み込み"));
      await userEvent.setup().click(screen.getByRole("button"));
      const dialog = within(await screen.findByRole("dialog"));
      const labels = {
        verified: "確認済み",
        verifying: "確認中（再試行 1/3）",
        pending: "確認待ち",
        failing: "失敗",
        empty: "コミットなし",
      };
      await waitFor(() =>
        expect(dialog.getAllByText(labels[name], { exact: true })).toHaveLength(1),
      );
      expect(dialog.getByRole("link", { name: /acme\/demo/ })).toBeInTheDocument();
      if (name === "empty") {
        expect(dialog.getByText("main にはまだコミットがありません。")).toBeInTheDocument();
        expect(dialog.queryByText("確認済みのコミットはまだない")).not.toBeInTheDocument();
      }
    },
  );
});
