import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose } from "../test/mantine";
import { applyChange, LOST_AFTER_MS } from "../query/events";
import { pull } from "./fixtures";

const page = (items: unknown[]) => json(200, { items, next_cursor: null });
const repo = (path: string) => ({ path, default_branch: "main", clone_url: "", url: "", protected_branches: [], required_checks: [], required_approvals: 0, labels: [], upstream: null, revision: 1 });

const SHELL = {
  "GET /api/v1/session": json(200, SESSION),
  "GET /api/v1/workspaces": page([
    { name: "acme", lanes: ["new"] },
    { name: "beta", lanes: ["new"] },
  ]),
  "GET /api/v1/admin/diagnostics": json(200, {}),
  "GET /api/v1/members": page([]),
};

it.each(["en", "ja"] as const)("explains service path skips in the check and attempt (%s)", async (lang) => {
  const sha = "d".repeat(40), base = "b".repeat(40), mergeBase = "c".repeat(40);
  const path_skip = {paths:["src/**", "tests/*.rs"], bases:[{pull:3,base:"main",base_sha:base,merge_base:mergeBase}]};
  fakeApi({...SHELL,
    [`GET /api/v1/repos/acme/demo/commits/${sha}`]:json(200,{sha,message:"Documentation",author:"Test",authored_at:"2026-10-27T00:00:00Z",parents:[base]}),
    [`GET /api/v1/repos/acme/demo/commits/${sha}/checks`]:json(200,{sha,checks_expected:true,checks:[{name:"test",status:"completed",conclusion:"skipped",path_skip,attempt_id:"att_skip",attempt:1}]}),
    "GET /api/v1/repos/acme/demo/jobs/att_skip":json(200,{id:"att_skip",purpose:"check",job:"test",attempt:1,sha,status:"completed",conclusion:"skipped",path_skip,artifact_names:[],details:null}),
    "GET /api/v1/repos/acme/demo/jobs/att_skip/log":json(200,{log:"[branvil] skipped: no file matching paths changed since merge-base",log_bytes:70,truncated:false}),
  });
  const user = userEvent.setup();
  renderApp(`/acme/demo/-/commits/${sha}`,lang);
  const label = lang === "ja" ? "省略（変更なし）" : "Skipped (no changes)";
  expect(await screen.findByText(label)).toBeInTheDocument();
  expect(screen.getByText(/src\/\*\*, tests\/\*\.rs/)).toBeInTheDocument();
  expect(screen.getByTitle(mergeBase).closest("a")).toHaveAttribute("href",`/acme/demo/-/commits/${mergeBase}`);
  await user.click(screen.getByRole("button",{name:lang === "ja" ? "チェック test を開く" : "Open check test"}));
  const drawer = await screen.findByRole("dialog");
  expect(await within(drawer).findByText(/src\/\*\*, tests\/\*\.rs/)).toBeInTheDocument();
  expect(within(drawer).getByText(label)).toBeInTheDocument();
  expect(within(drawer).getByRole("button",{name:lang === "ja" ? "再実行" : "Retry"})).toBeEnabled();
});

/** The page's change stream, opened and dropped by the test. */
class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static last: FakeEventSource | null = null;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    FakeEventSource.last = this;
  }
  addEventListener() {}
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
  open() {
    this.readyState = FakeEventSource.OPEN;
    act(() => this.onopen?.());
  }
  /** Dropped, the browser retrying it as EventSource does. */
  drop() {
    this.readyState = FakeEventSource.CONNECTING;
    act(() => this.onerror?.());
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("pages", () => {
  it("opens the first workspace's task flow, then the one shown last", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/workspaces/acme": json(200, { name: "acme", lanes: ["new"] }),
      "GET /api/v1/workspaces/acme/board/cards": page([]),
      "GET /api/v1/workspaces/beta": json(200, { name: "beta", lanes: ["new"] }),
      "GET /api/v1/workspaces/beta/board/cards": page([]),
      "GET /api/v1/repos": page([]),
    });
    const first = renderApp("/");
    await waitFor(() => expect(window.location.pathname).toBe("/acme"));
    first.unmount();
    renderApp("/beta");
    expect(await screen.findByText("beta · 0 open")).toBeInTheDocument();
    renderApp("/");
    await waitFor(() => expect(window.location.pathname).toBe("/beta"));
  });

  it("offers to create a workspace when there is none", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/workspaces": page([]) });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "No workspace yet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New workspace" })).toBeInTheDocument();
  });

  it("lists the pull requests of the workspace's repositories, newest first, and narrows them to one repository", async () => {
    const older = pull({ updated_at: "2026-10-13T10:00:00.000Z" });
    const newer = pull({
      url: `${window.location.origin}/acme/web/-/pulls/1`,
      repository: "acme/web",
      number: 1,
      label: "!1",
      title: "Draft work",
      draft: true,
      landing: null,
      updated_at: "2026-10-15T10:00:00.000Z",
    });
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo("acme/demo"), repo("acme/web"), repo("beta/x")]),
      "GET /api/v1/repos/acme/demo/pulls": page([older]),
      "GET /api/v1/repos/acme/web/pulls": page([newer]),
      "GET /api/v1/repos/acme/demo/pulls/3/checks": json(200, { head_sha: "a", base_sha: "b", behind_by: 0, checks: [], checks_expected: false, base_checks: 0 }),
      "GET /api/v1/repos/acme/web/pulls/1/checks": json(200, { head_sha: "a", base_sha: "b", behind_by: 0, checks: [], checks_expected: false, base_checks: 0 }),
    });
    const user = userEvent.setup();
    renderApp("/acme/-/pulls");
    const table = await screen.findByRole("table", { name: "Pull requests" });
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(3));
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => r.getAttribute("aria-label"))).toEqual(["acme/web !1 Draft work", "acme/demo !3 Fix login"]);
    expect(rows[1]).toHaveAttribute("href", "/acme/demo/-/pulls/3");
    expect(await within(rows[0] as HTMLElement).findByText(/Not queued: Draft/)).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "Reviewers" })).toBeInTheDocument();
    for (const row of rows) {
      expect(within(row).getByRole("img", { name: "Reviewers: owner (Requested)" })).toBeInTheDocument();
      expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    }
    // Another workspace's repository is not read.
    expect(fake.calls("GET", "/api/v1/repos/beta/x/pulls")).toHaveLength(0);

    // A repository's page is this list for that repository.
    await choose(user, "Repository", "acme/demo");
    await waitFor(() => expect(window.location.pathname).toBe("/acme/demo"));
    await waitFor(() => expect(within(screen.getByRole("table", { name: "Pull requests" })).getAllByRole("row")).toHaveLength(2));

    await choose(user, "State", "State: Closed");
    await waitFor(() => expect(fake.calls("GET", "/api/v1/repos/acme/demo/pulls").some((c) => c.search.includes("state=closed"))).toBe(true));
  });

  it("says when no pull request is open", async () => {
    fakeApi({ ...SHELL, "GET /api/v1/repos": page([repo("acme/demo")]), "GET /api/v1/repos/acme/demo/pulls": page([]) });
    renderApp("/acme/-/pulls");
    expect(await screen.findByText("No open pull requests.")).toBeInTheDocument();
  });

  it("shows a commit's message, author, and parents", async () => {
    const sha = "d".repeat(40);
    const parent = "e".repeat(40);
    fakeApi({
      ...SHELL,
      [`GET /api/v1/repos/acme/demo/commits/${sha}`]: json(200, {
        sha,
        message: "Fix login\n\nHandle an empty token.",
        author: "Aiko <aiko@example.com>",
        authored_at: "2026-10-14T10:00:00.000Z",
        parents: [parent],
      }),
      [`GET /api/v1/repos/acme/demo/commits/${sha}/checks`]: json(200, { sha, checks: [], checks_expected: false }),
    });
    renderApp(`/acme/demo/-/commits/${sha}`);
    expect(await screen.findByRole("heading", { name: "Fix login" })).toBeInTheDocument();
    expect(screen.getByText("Handle an empty token.")).toBeInTheDocument();
    expect(screen.getByText("Aiko <aiko@example.com>")).toBeInTheDocument();
    expect(screen.getByTitle(parent).closest("a")).toHaveAttribute("href", `/acme/demo/-/commits/${parent}`);
    const checks = await screen.findByRole("region", { name: "Checks" });
    expect(await within(checks).findByText("This repository runs no checks for this commit.")).toBeInTheDocument();
  });

  it("shows a commit's checks on its page and opens a job from them", async () => {
    const sha = "d".repeat(40);
    const fake = fakeApi({
      ...SHELL,
      [`GET /api/v1/repos/acme/demo/commits/${sha}`]: json(200, { sha, message: "Fix login", author: "Aiko", authored_at: "2026-10-14T10:00:00.000Z", parents: [] }),
      [`GET /api/v1/repos/acme/demo/commits/${sha}/checks`]: json(200, {
        sha,
        checks: [
          { name: "test", status: "completed", conclusion: "failure", attempt_id: "att_1", attempt: 1, artifact_names: [] },
          { name: "lint", status: "queued", conclusion: null },
        ],
        checks_expected: true,
      }),
      "GET /api/v1/repos/acme/demo/jobs/att_1": json(200, { id: "att_1", job: "test", attempt: 1, sha, status: "completed", conclusion: "failure", details: "exit code 101", artifact_names: ["report"] }),
      "GET /api/v1/repos/acme/demo/jobs/att_1/log": json(200, { log: "test login ... FAILED", log_bytes: 20, truncated: false }),
    });
    const user = userEvent.setup();
    renderApp(`/acme/demo/-/commits/${sha}`);
    const checks = await screen.findByRole("region", { name: "Checks" });
    expect(await within(checks).findByText("failing")).toBeInTheDocument();
    expect(within(checks).getByText("queued")).toBeInTheDocument();
    expect(await within(checks).findByText("Reason: exit code 101")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: sha })).toHaveAttribute("href", `/acme/demo/-/commits/${sha}`);
    expect(within(checks).getByTitle(sha).closest("a")).toHaveAttribute("href", `/acme/demo/-/commits/${sha}`);

    await user.click(within(checks).getByRole("button", { name: "Open check test" }));
    const job = await screen.findByRole("dialog", { name: /test/ });
    expect(await within(job).findByText("test login ... FAILED")).toBeInTheDocument();
    expect(within(job).getByRole("link", { name: "report" })).toHaveAttribute("href", `/api/v1/repos/acme%2Fdemo/commits/${sha}/artifacts/report`);
    expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${sha}/checks`)).toHaveLength(1);
  });

  it.each(["en", "ja"] as const)("links a reused check and attempt to its source execution (%s)", async (language) => {
    const sha = "d".repeat(40);
    const source = { attempt_id: "att_original", url: `${window.location.origin}/acme/demo/-/jobs/att_original`, sha: "e".repeat(40) };
    fakeApi({
      ...SHELL,
      [`GET /api/v1/repos/acme/demo/commits/${sha}`]: json(200, { sha, message: "Same tree", author: "Test", authored_at: "2026-10-14T10:00:00.000Z", parents: [] }),
      [`GET /api/v1/repos/acme/demo/commits/${sha}/checks`]: json(200, { sha, checks_expected: true, checks: [
        { name: "test", status: "completed", conclusion: "success", attempt_id: "att_reused", attempt: 1, details: null, artifact_names: ["report"], reused_from: source, cache_refresh: "att_refresh", purpose: "check" },
      ] }),
      "GET /api/v1/repos/acme/demo/jobs/att_reused": json(200, { id: "att_reused", job: "test", attempt: 1, sha, status: "completed", conclusion: "success", details: null, artifact_names: ["report"], reused_from: source, cache_refresh: "att_refresh", purpose: "check" }),
      "GET /api/v1/repos/acme/demo/jobs/att_reused/log": json(200, { log: "reused result of att_original", log_bytes: 28, truncated: false }),
    });
    const user = userEvent.setup();
    renderApp(`/acme/demo/-/commits/${sha}`, language);
    const label = language === "ja" ? "再利用元" : "Reused from";
    const link = await screen.findByRole("link", { name: new RegExp(label) });
    expect(link).toHaveAttribute("href", "/acme/demo/-/jobs/att_original");
    expect(screen.getByRole("link", { name: source.sha.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${source.sha}`);
    expect(link.querySelector("a")).toBeNull();
    expect(screen.getByRole("link", { name: language === "ja" ? "キャッシュの更新" : "Cache refresh" })).toHaveAttribute("href", "/acme/demo/-/jobs/att_refresh");
    const reason = language === "ja" ? "理由" : "Reason";
    expect(screen.queryByText(new RegExp(reason))).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: language === "ja" ? "チェック test を開く" : "Open check test" }));
    const drawer = await screen.findByRole("dialog", { name: /test/ });
    expect(await within(drawer).findByText(label)).toBeInTheDocument();
    expect(within(drawer).queryByText(reason)).not.toBeInTheDocument();
    expect(within(drawer).getByRole("link", { name: /att_original/ })).toHaveAttribute("href", "/acme/demo/-/jobs/att_original");
    expect(within(drawer).getByRole("link", { name: source.sha.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${source.sha}`);
    expect(within(drawer).getByRole("link", { name: /att_original/ }).querySelector("a")).toBeNull();
    expect(within(drawer).getByRole("link", { name: "report" })).toHaveAttribute("href", `/api/v1/repos/acme%2Fdemo/commits/${sha}/artifacts/report`);
    expect(await within(drawer).findByText("reused result of att_original")).toBeInTheDocument();
  });

  it.each(["en", "ja"] as const)("identifies a cache refresh and disables manual retry (%s)", async (language) => {
    const sha = "f".repeat(40);
    fakeApi({
      ...SHELL,
      "GET /api/v1/repos/acme/demo/pulls": page([]),
      [`GET /api/v1/repos/acme/demo/commits/${sha}`]: json(200, { sha, message: "Same tree", author: "Test", parents: [] }),
      [`GET /api/v1/repos/acme/demo/commits/${sha}/checks`]: json(200, { sha, checks_expected: true, checks: [] }),
      "GET /api/v1/repos/acme/demo/jobs/att_refresh": json(200, { id: "att_refresh", job: "test", purpose: "cache_refresh", attempt: 2, sha, status: "completed", conclusion: "failure", details: "exit 1", artifact_names: [], reused_from: null }),
      "GET /api/v1/repos/acme/demo/jobs/att_refresh/log": json(200, { log: "cache refresh failed", log_bytes: 20, truncated: false }),
    });
    renderApp("/acme/demo/-/jobs/att_refresh", language);
    const drawer = await screen.findByRole("dialog", { name: /test/ });
    expect(await within(drawer).findByText(language === "ja" ? "キャッシュの更新" : "Cache refresh")).toBeInTheDocument();
    expect(within(drawer).getByRole("button", { name: language === "ja" ? "再実行" : "Retry" })).toBeDisabled();
    expect(within(drawer).getByText(language === "ja" ? "この結果はチェックと自動更新に影響しません。" : "This result does not affect checks or automatic updates.")).toBeInTheDocument();
  });

  it("shows the checks of the default branch's tip on a repository's page", async () => {
    const tip = "f".repeat(40);
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo("acme/demo")]),
      "GET /api/v1/repos/acme/demo": json(200, { ...repo("acme/demo"), default_branch: "trunk" }),
      "GET /api/v1/repos/acme/demo/branches/trunk": json(200, { name: "trunk", sha: tip }),
      [`GET /api/v1/repos/acme/demo/commits/${tip}/checks`]: json(200, {
        sha: tip,
        checks: [{ name: "test", status: "in_progress", conclusion: null, attempt_id: "att_2", attempt: 1 }],
        checks_expected: true,
      }),
      "GET /api/v1/repos/acme/demo/pulls": page([]),
    });
    renderApp("/acme/demo");
    await userEvent.setup().click(await screen.findByRole("button", { name: /^Checks of trunk/ }));
    const checks = await screen.findByRole("region", { name: "Checks of trunk" });
    expect(await within(checks).findByText("pending")).toBeInTheDocument();
    expect(within(checks).getByText("in progress")).toBeInTheDocument();
    expect(within(checks).getByTitle(tip).closest("a")).toHaveAttribute("href", `/acme/demo/-/commits/${tip}`);
    expect(within(checks).getByRole("button", { name: "Open check test" })).toBeInTheDocument();
    expect(await screen.findByText("No open pull requests.")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/branches/trunk")).toHaveLength(1);
  });

  it("says so on a repository's page when its default branch has no commit yet", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo("acme/demo")]),
      "GET /api/v1/repos/acme/demo": json(200, repo("acme/demo")),
      "GET /api/v1/repos/acme/demo/pulls": page([]),
    });
    renderApp("/acme/demo");
    await userEvent.setup().click(await screen.findByRole("button", { name: /^Checks of main/ }));
    expect(await screen.findByText("main has no commits yet.")).toBeInTheDocument();
  });

  it("summarizes the default branch checks in the page header, apart from the list, and opens them in a drawer", async () => {
    const tip = "f".repeat(40);
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo("acme/demo")]),
      "GET /api/v1/repos/acme/demo": json(200, repo("acme/demo")),
      "GET /api/v1/repos/acme/demo/branches/main": json(200, { name: "main", sha: tip }),
      [`GET /api/v1/repos/acme/demo/commits/${tip}/checks`]: json(200, { sha: tip, checks: [], checks_expected: false }),
      "GET /api/v1/repos/acme/demo/pulls": page([]),
    });
    renderApp("/acme/-/pulls");
    const list = await screen.findByRole("table", { name: "Pull requests" });
    await waitFor(() => expect(fake.calls("GET", `/api/v1/repos/acme/demo/commits/${tip}/checks`)).toHaveLength(1));
    const summary = screen.getByRole("button", { name: /Default branch CI/, expanded: false });
    expect(summary.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(summary.closest(".mantine-Card-root")).toBeNull();
    const user = userEvent.setup();
    await user.click(summary);
    const sha = await screen.findByTitle(tip);
    expect(sha.closest("a")).toHaveAttribute("href", `/acme/demo/-/commits/${tip}`);
  });

  it("refreshes review status in the list after a pull event, preserving row navigation", async () => {
    const pr = pull({ reviewers: [], review_states: [] });
    const fake = fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([repo("acme/demo")]),
      "GET /api/v1/repos/acme/demo/pulls": [page([pr]), page([{ ...pr, review_states: [
        { member: "review-bot", requested: false, verdict: "approve", outdated: false },
      ] }])],
    });
    const { client } = renderApp("/acme/-/pulls");
    const row = await screen.findByRole("row", { name: "acme/demo !3 Fix login" });
    expect(await within(row).findByRole("img", { name: "Reviewers: no one" })).toBeInTheDocument();
    act(() => applyChange({ kind: "pull", path: "acme/demo", id: "3" }, client));
    const status = await within(row).findByRole("img", { name: "Reviewers: review-bot (Approved)" });
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/pulls")).toHaveLength(2);
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/pulls/3/activity")).toHaveLength(0);
    await userEvent.setup().click(status);
    expect(window.location.pathname).toBe("/acme/demo/-/pulls/3");
  });

  it("warns only when CI cannot run or an upstream cannot be followed, and a dismissed warning stays away", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/repos": page([]),
      "GET /api/v1/admin/diagnostics": json(200, {
        runner: { state: "no_runner", detail: "No runner has contacted Branvil.", mode: "auto" },
        upstream: { state: "problems", problems: [{ repository: "acme/demo", problem: "The reader's token was refused." }] },
      }),
    });
    const user = userEvent.setup();
    renderApp("/-/settings/members");
    const warnings = await screen.findByRole("region", { name: "Warnings" });
    expect(await within(warnings).findByText(/No CI runner is connected/)).toBeInTheDocument();
    expect(within(warnings).getByText("The upstream of acme/demo cannot be followed: The reader's token was refused.")).toBeInTheDocument();
    expect(within(warnings).getByRole("link", { name: "Open the repository settings" })).toHaveAttribute("href", "/-/settings");
    await user.click(within(warnings).getAllByRole("button", { name: "Dismiss" })[0] as HTMLElement);
    expect(within(warnings).queryByText(/No CI runner is connected/)).not.toBeInTheDocument();
    expect(within(warnings).getByText(/cannot be followed/)).toBeInTheDocument();
  });

  it("warns while Branvil waits for the running CI jobs to stop, also when CI runs on another machine", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/members": page([]),
      "GET /api/v1/admin/diagnostics": json(200, {
        runner: { state: "stopping", detail: "Branvil is stopping.", mode: "off" },
        upstream: { state: "following", problems: [] },
      }),
    });
    renderApp("/-/settings/members");
    const warnings = await screen.findByRole("region", { name: "Warnings" });
    expect(await within(warnings).findByText(/Branvil is stopping for an update or a restart once the running CI jobs finish/)).toBeInTheDocument();
  });

  it("waits for service warnings beyond the former one-second query deadline", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/admin/diagnostics": async () => {
        await new Promise((resolve) => setTimeout(resolve, 1_100));
        return json(200, { runner: { state: "stopping", detail: "Branvil is stopping.", mode: "off" } });
      },
    });
    renderApp("/-/settings/members");
    const warnings = await screen.findByRole("region", { name: "Warnings" });
    expect(within(warnings).getByText(/Branvil is stopping for an update or a restart once the running CI jobs finish/)).toBeInTheDocument();
  });

  it("does not warn about the runner when CI is turned off", async () => {
    fakeApi({
      ...SHELL,
      "GET /api/v1/members": page([]),
      "GET /api/v1/admin/diagnostics": json(200, { runner: { state: "no_runner", detail: "", mode: "off" }, upstream: { state: "following", problems: [] } }),
    });
    renderApp("/-/settings/members");
    await screen.findByRole("list", { name: "Members" });
    expect(screen.queryByRole("region", { name: "Warnings" })).not.toBeInTheDocument();
  });
  it("says nothing while the page is connected, and warns once the connection has been lost for a while", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.stubGlobal("EventSource", FakeEventSource);
    // This page test has one document; cross-tab exclusivity and delivery are
    // covered by changeStream.test.tsx. jsdom does not implement Web Locks.
    vi.stubGlobal("navigator", { locks: {
      request: async (_name: string, _options: LockOptions, callback: () => Promise<void>) => callback(),
    } });
    fakeApi({ ...SHELL, "GET /api/v1/admin/diagnostics": json(200, { upstream: { state: "following", problems: [] } }) });
    renderApp("/-/settings/members");
    await screen.findByRole("list", { name: "Members" });
    const stream = FakeEventSource.last as FakeEventSource;
    stream.open();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    // A moment's drop, back before the warning is due, is not worth one.
    stream.drop();
    act(() => vi.advanceTimersByTime(LOST_AFTER_MS - 1));
    stream.open();
    act(() => vi.advanceTimersByTime(LOST_AFTER_MS));
    expect(screen.queryByRole("region", { name: "Warnings" })).not.toBeInTheDocument();

    // A longer one is, counted from its own start, and cannot be dismissed.
    stream.drop();
    act(() => vi.advanceTimersByTime(LOST_AFTER_MS - 1));
    expect(screen.queryByRole("region", { name: "Warnings" })).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    const warnings = screen.getByRole("region", { name: "Warnings" });
    expect(within(warnings).getByText("The connection to the server is lost, so what you see may be out of date. Reconnecting…")).toBeInTheDocument();
    expect(within(warnings).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();

    stream.open();
    expect(screen.queryByRole("region", { name: "Warnings" })).not.toBeInTheDocument();
  });
});
