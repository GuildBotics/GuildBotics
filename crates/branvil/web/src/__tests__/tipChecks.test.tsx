import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import { applyChange } from "../query/events";
import { createQueryClient } from "../query/hooks";
import { apiError, fakeApi, json } from "../test/fakeApi";
import { theme } from "../theme";
import { TipChecks } from "../views/pulls/TipChecks";

const SHA = "a".repeat(40);
const NEXT = "b".repeat(40);
const check = (status = "completed", conclusion: string | null = "success") => ({ name: "test", status, conclusion });
const checks = (items = [check()], expected = true, sha = SHA) => json(200, { sha, checks: items, checks_expected: expected });
const repoRoutes = (path: string, result = checks()) => ({
  [`GET /api/v1/repos/${path}`]: json(200, { path, default_branch: "trunk" }),
  [`GET /api/v1/repos/${path}/branches/trunk`]: json(200, { name: "trunk", sha: SHA }),
  [`GET /api/v1/repos/${path}/commits/${SHA}/checks`]: result,
});

function renderChecks(paths: string[], repo: string | null = null, language: "en" | "ja" = "en") {
  const client = createQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: Infinity } });
  render(
    <MantineProvider theme={theme} env="test">
      <QueryClientProvider client={client}>
        <I18nProvider initial={language}><TipChecks workspace="acme" paths={paths} repo={repo} /></I18nProvider>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return client;
}

describe("default branch CI in the pull list header", () => {
  it("loads and counts all CI states while closed, and distinguishes missing checks, empty repositories and read failures", async () => {
    const paths = ["passing", "failing", "pending", "missing", "none", "empty", "error", "loading"].map((name) => `acme/${name}`);
    fakeApi({
      ...repoRoutes(paths[0]!),
      ...repoRoutes(paths[1]!, checks([check("completed", "failure")])),
      ...repoRoutes(paths[2]!, checks([check("in_progress", null)])),
      ...repoRoutes(paths[3]!, checks([])),
      ...repoRoutes(paths[4]!, checks([], false)),
      ...repoRoutes(paths[5]!),
      [`GET /api/v1/repos/${paths[5]}/branches/trunk`]: apiError(404, "not_found", "No branch"),
      ...repoRoutes(paths[6]!),
      [`GET /api/v1/repos/${paths[6]}/commits/${SHA}/checks`]: apiError(403, "forbidden", "Checks denied"),
      ...repoRoutes(paths[7]!),
      [`GET /api/v1/repos/${paths[7]}/commits/${SHA}/checks`]: () => new Promise(() => {}),
    });
    const user = userEvent.setup();
    renderChecks(paths);
    const toggle = screen.getByRole("button", { name: /Default branch CI/ });
    await waitFor(() => {
      for (const label of ["passing 1", "failing 1", "pending 1", "not registered yet 1", "no checks 1", "no commits 1", "could not load 1", "Loading… 1"]) expect(toggle).toHaveTextContent(label);
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("link", { name: /^acme\/passing\b/ })).not.toBeInTheDocument();
    await user.click(toggle);
    const section = await screen.findByRole("region", { name: "Default branch CI of acme" });
    expect(within(section).getAllByRole("link", { name: /^acme\// })).toHaveLength(8);
    expect(within(section).getByText(/Checks denied/)).toBeInTheDocument();
  });

  it("reads at most 30 repositories and announces the repositories omitted from the aggregate", async () => {
    const paths = Array.from({ length: 32 }, (_, i) => `acme/repo-${i}`);
    const fake = fakeApi(Object.assign({}, ...paths.slice(0, 30).map((path) => repoRoutes(path))));
    renderChecks(paths);
    const toggle = screen.getByRole("button", { name: /Default branch CI/ });
    await waitFor(() => expect(toggle).toHaveTextContent("passing 30"));
    await userEvent.setup().click(toggle);
    expect(await screen.findByText("Checks are loaded for the first 30 repositories; 2 more are omitted.")).toBeInTheDocument();
    expect(fake.requests).toHaveLength(90);
    expect(fake.requests.some((request) => /repo-3[01](?:\/|$)/.test(request.path))).toBe(false);
  });

  it("refetches checks and branch tips on events while folded, and links repositories and commits separately", async () => {
    vi.spyOn(window, "scrollTo").mockImplementation(() => {});
    let sha = SHA;
    let result = checks();
    const fake = fakeApi({
      ...repoRoutes("acme/demo"),
      "GET /api/v1/repos/acme/demo/branches/trunk": () => json(200, { name: "trunk", sha }),
      [`GET /api/v1/repos/acme/demo/commits/${SHA}/checks`]: () => result,
      [`GET /api/v1/repos/acme/demo/commits/${NEXT}/checks`]: checks([check("in_progress", null)], true, NEXT),
    });
    const user = userEvent.setup();
    const client = renderChecks(["acme/demo"]);
    const toggle = screen.getByRole("button", { name: /Default branch CI/ });
    await waitFor(() => expect(toggle).toHaveTextContent("passing 1"));
    result = checks([check("completed", "failure")]);
    act(() => applyChange({ kind: "check", path: "acme/demo", id: SHA }, client));
    await waitFor(() => expect(toggle).toHaveTextContent("failing 1"));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${SHA}/checks`)).toHaveLength(2);
    sha = NEXT;
    act(() => applyChange({ kind: "repo", path: "acme/demo", id: "" }, client));
    await waitFor(() => expect(toggle).toHaveTextContent("pending 1"));
    await user.click(toggle);
    const section = await screen.findByRole("region", { name: "Default branch CI of acme" });
    expect(within(section).getByText("trunk")).toBeInTheDocument();
    await user.click(within(section).getByRole("link", { name: "acme/demo trunk pending" }));
    expect(window.location.pathname).toBe("/acme/demo");
    window.history.replaceState(null, "", "/acme/-/pulls");
    await user.click(within(section).getByText("trunk"));
    expect(window.location.pathname).toBe("/acme/demo");
    window.history.replaceState(null, "", "/acme/-/pulls");
    await user.click(within(section).getByText("pending"));
    expect(window.location.pathname).toBe("/acme/demo");
    const commit = within(section).getByRole("link", { name: NEXT.slice(0, 10) });
    expect(commit.parentElement?.closest("a")).toBeNull();
    await user.click(commit);
    expect(window.location.pathname).toBe(`/acme/demo/-/commits/${NEXT}`);
  });

  it("replaces cached success with a read error when a refresh fails", async () => {
    const fake = fakeApi({
      ...repoRoutes("acme/demo"),
      [`GET /api/v1/repos/acme/demo/commits/${SHA}/checks`]: [checks(), apiError(403, "forbidden", "Checks denied")],
    });
    const client = renderChecks(["acme/demo"]);
    const toggle = screen.getByRole("button", { name: /Default branch CI/ });
    await waitFor(() => expect(toggle).toHaveTextContent("passing 1"));
    act(() => applyChange({ kind: "check", path: "acme/demo", id: SHA }, client));
    await waitFor(() => expect(toggle).toHaveTextContent("could not load 1"));
    expect(toggle).not.toHaveTextContent("passing");
    expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${SHA}/checks`)).toHaveLength(2);
  });

  it("summarizes the selected repository's tip and opens its jobs and a job's drawer, with a commit link", async () => {
    fakeApi({
      ...repoRoutes("acme/demo"),
      [`GET /api/v1/repos/acme/demo/commits/${SHA}/checks`]: json(200, { sha: SHA, checks_expected: true, checks: [{ ...check(), attempt_id: "hosted-test", details_url: "https://example.test/check" }] }),
      "GET /api/v1/repos/acme/demo/jobs/hosted-test/log": json(200, { log: "test output", log_bytes: 11, truncated: false }),
    });
    const user = userEvent.setup();
    renderChecks(["acme/demo"], "acme/demo");
    const toggle = await screen.findByRole("button", { name: /^Checks of trunk/ });
    await waitFor(() => expect(toggle).toHaveTextContent("passing"));
    expect(toggle).not.toHaveTextContent("passing 1");
    await user.click(toggle);
    const region = await screen.findByRole("region", { name: "Checks of trunk" });
    expect((await within(region).findByTitle(SHA)).closest("a")).toHaveAttribute("href", `/acme/demo/-/commits/${SHA}`);
    await user.click(await within(region).findByRole("button", { name: "Open check test" }));
    await waitFor(() => expect(screen.getAllByRole("dialog")).toHaveLength(2));
    expect(screen.getByRole("link", { name: "Open on the host" })).toHaveAttribute("href", "https://example.test/check");
    expect(await screen.findByText("test output")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Default branch CI/ })).not.toBeInTheDocument();
  });

  it("keeps the selected repository's no-tip message and Japanese labels", async () => {
    fakeApi({ ...repoRoutes("acme/demo"), "GET /api/v1/repos/acme/demo/branches/trunk": apiError(404, "not_found", "No branch") });
    renderChecks(["acme/demo"], "acme/demo", "ja");
    await userEvent.setup().click(await screen.findByRole("button", { name: /^trunk のチェック/ }));
    expect(await screen.findByText("trunk にはまだコミットがありません。")).toBeInTheDocument();
  });
});
