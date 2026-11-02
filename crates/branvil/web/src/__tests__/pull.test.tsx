import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Check, Checks } from "../api/types";
import { dictionaries, format } from "../i18n";
import { applyChange } from "../query/events";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { BASE, HEAD, NEW_HEAD, card, pull } from "./fixtures";

const P = "/api/v1/repos/acme/demo/pulls/3";
const SUCCESS: Check = { name: "test", status: "completed", conclusion: "success" };
const CHECKS: Checks = { head_sha: HEAD, base_sha: BASE, behind_by: 0, checks: [SUCCESS], checks_expected: true, base_checks: 1 };

describe.each(["en", "ja"] as const)("base branch guidance (%s)", (language) => {
  it.each([
    { state: "open", merged: false, draft: false, behind: 1, visible: true },
    { state: "open", merged: false, draft: true, behind: 3, visible: true },
    { state: "open", merged: false, draft: false, behind: 0, visible: false },
    { state: "closed", merged: false, draft: false, behind: 3, visible: false },
    { state: "closed", merged: true, draft: false, behind: 1, visible: false },
    { state: "open", merged: true, draft: false, behind: 1, visible: false },
  ] as const)("state=$state merged=$merged draft=$draft behind=$behind", async ({ state, merged, draft, behind, visible }) => {
    const base = "release/next";
    fakeApi(pullRoutes({
      [`GET ${P}`]: json(200, pull({ state, merged, draft, base })),
      [`GET ${P}/checks`]: json(200, { ...CHECKS, behind_by: behind }),
    }));
    renderApp("/acme/demo/-/pulls/3", language);
    const checks = within(await screen.findByRole("region", { name: dictionaries[language]["pull.checks"] }));
    // Wait for the checks response before asserting absence; historical CI remains visible.
    expect(await checks.findByText("test")).toBeInTheDocument();
    const message = format(dictionaries[language]["pull.behindBy"], { base, count: behind });
    if (visible) expect(checks.getByText(message)).toBeInTheDocument();
    else expect(checks.queryByText(message)).not.toBeInTheDocument();
  });
});

describe("related ticket editor", () => {
  it.each(["en", "ja"] as const)("adds, removes and switches links with the opened revision (%s)", async (language) => {
    const words = dictionaries[language];
    const local = card("12", "ready");
    const other = card("7", "done", { repository: "acme/other", state: "closed" });
    const removed = card("1", "ready");
    // The service's URL can differ from the browser's dev proxy origin.
    const service = "http://127.0.0.1:7878";
    for (const c of [local, other, removed]) c.url = `${service}/${c.repository}/-/tickets/${c.key}`;
    const original = pull({ url: `${service}/acme/demo/-/pulls/3`, tickets: [{ url: removed.url, closes: true }], revision: 4 });
    const tickets = [{ url: local.url, closes: true }, { url: other.url, closes: false }];
    const edited = { ...original, tickets, revision: 5 };
    const fake = fakeApi(pullRoutes({
      [`GET ${P}`]: [json(200, original), json(200, edited)],
      "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [local, other, removed], next_cursor: null }),
      [`PATCH ${P}`]: json(200, edited),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3", language);
    await user.click(await screen.findByRole("button", { name: words["pull.editTickets"] }));
    const form = screen.getByRole("form", { name: words["pull.editTickets"] });
    expect(await within(form).findByRole("link", { name: `acme/demo#1 ${removed.title}` })).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: words["pull.removeTicket"].replace("{ticket}", "acme/demo#1") }));
    for (const c of [local, other]) {
      await user.click(await within(form).findByRole("combobox", { name: words["pull.addTicket"] }));
      const name = `${c.repository}#${c.key} ${c.title}`;
      await user.click(await screen.findByRole("option", { name: c.state === "closed" ? `${name} (${words["ticket.closed"]})` : name }));
      expect(within(form).getByRole("link", { name })).toBeInTheDocument();
    }
    const localRow = within(form).getByRole("group", { name: "acme/demo#12" });
    await user.click(within(localRow).getByRole("checkbox", { name: words["pull.closeOnMerge"] }));
    const otherRow = within(form).getByRole("group", { name: "acme/other#7" });
    expect(within(otherRow).getByRole("checkbox")).toBeDisabled();
    expect(within(otherRow).getByText(words["pull.referenceOnly"])).toBeInTheDocument();
    await user.click(within(form).getByRole("button", { name: words["common.save"] }));
    await waitFor(() => expect(fake.calls("PATCH", P)[0]?.body).toEqual({ tickets, revision: 4 }));
    await waitFor(() => expect(screen.queryByRole("form", { name: words["pull.editTickets"] })).not.toBeInTheDocument());
    expect(screen.getByText(words["pull.closesTicket"])).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards")[0]?.search).toContain("state=all");
  });

  it.each(["en", "ja"] as const)("keeps the opened revision across events, explains a conflict, and reopens on the latest state (%s)", async (language) => {
    const words = dictionaries[language];
    const tk = card("12", "ready");
    const original = pull({ tickets: [{ url: tk.url, closes: false }], revision: 3 });
    const latest = pull({ tickets: [{ url: tk.url, closes: true }], revision: 4 });
    const fake = fakeApi(pullRoutes({
      [`GET ${P}`]: [json(200, original), json(200, latest)],
      "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [tk], next_cursor: null }),
      [`PATCH ${P}`]: apiError(409, "conflict", "Revision mismatch."),
    }));
    const user = userEvent.setup();
    const { client } = renderApp("/acme/demo/-/pulls/3", language);
    await user.click(await screen.findByRole("button", { name: words["pull.editTickets"] }));
    applyChange({ kind: "pull", path: "acme/demo", id: "3" }, client);
    await waitFor(() => expect(client.getQueryData(["repo", "acme/demo", "pull", 3])).toMatchObject({ revision: 4 }));
    const form = screen.getByRole("form", { name: words["pull.editTickets"] });
    expect(within(form).getByRole("checkbox")).not.toBeChecked();
    await user.click(within(form).getByRole("button", { name: words["common.save"] }));
    const alert = await within(form).findByRole("alert");
    expect(alert).toHaveTextContent(language === "ja" ? "編集中の変更は破棄されます" : "Canceling discards your edits");
    expect(alert).toHaveTextContent(language === "ja" ? "もう一度変更してください" : "reapply your changes");
    expect(fake.calls("PATCH", P)[0]?.body).toEqual({ tickets: original.tickets, revision: 3 });
    expect(within(form).getByRole("button", { name: words["common.save"] })).toBeDisabled();
    await user.click(within(form).getByRole("button", { name: words["common.cancel"] }));
    await user.click(screen.getByRole("button", { name: words["pull.editTickets"] }));
    expect(within(screen.getByRole("form", { name: words["pull.editTickets"] })).getByRole("checkbox")).toBeChecked();
  });

  it("can remove every link, including a foreign reference", async () => {
    const url = "https://example.com/tickets/4";
    const original = pull({ tickets: [{ url, closes: false }] });
    const fake = fakeApi(pullRoutes({
      [`GET ${P}`]: [json(200, original), json(200, pull({ revision: 2 }))],
      "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [], next_cursor: null }),
      [`PATCH ${P}`]: json(200, pull({ revision: 2 })),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    await user.click(await screen.findByRole("button", { name: dictionaries.en["pull.editTickets"] }));
    const form = screen.getByRole("form", { name: dictionaries.en["pull.editTickets"] });
    expect(within(form).getByRole("checkbox")).toBeDisabled();
    expect(within(form).getByRole("link", { name: url })).toHaveAttribute("href", url);
    await user.click(within(form).getByRole("button", { name: `Remove ${url}` }));
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fake.calls("PATCH", P)[0]?.body).toEqual({ tickets: [], revision: 1 }));
    expect(await screen.findByText(dictionaries.en["pull.noTickets"])).toBeInTheDocument();
  });

  it.each(["en", "ja"] as const)("keeps a noncanonical loopback alias as a reference (%s)", async (language) => {
    const words = dictionaries[language];
    fakeApi(pullRoutes({
      [`GET ${P}`]: json(200, pull({ url: "http://127.0.0.1:7878/acme/demo/-/pulls/3", tickets: [{ url: "http://localhost:7878/acme/demo/-/tickets/12", closes: false }] })),
      "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [], next_cursor: null }),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3", language);
    await user.click(await screen.findByRole("button", { name: words["pull.editTickets"] }));
    const form = screen.getByRole("form", { name: words["pull.editTickets"] });
    expect(within(form).getByRole("checkbox")).toBeDisabled();
    expect(within(form).getByText(language === "ja" ? "参照のみ（このリンクはマージで閉じられません）" : "Reference only (this link cannot be closed on merge)")).toBeInTheDocument();
  });

  it("waits for the conflict refresh before allowing the editor to reopen", async () => {
    const tk = card("12", "ready");
    let refresh!: (value: ReturnType<typeof json>) => void;
    fakeApi(pullRoutes({
      [`GET ${P}`]: [json(200, pull()), () => new Promise((resolve) => { refresh = resolve; })],
      "GET /api/v1/workspaces/acme/board/cards": json(200, { items: [tk], next_cursor: null }),
      [`PATCH ${P}`]: apiError(409, "conflict", "Revision mismatch."),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    await user.click(await screen.findByRole("button", { name: dictionaries.en["pull.editTickets"] }));
    const form = screen.getByRole("form", { name: dictionaries.en["pull.editTickets"] });
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(refresh).toBeTypeOf("function"));
    expect(within(form).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await act(async () => refresh(json(200, pull({ revision: 5, tickets: [{ url: tk.url, closes: true }] }))));
    await within(form).findByRole("alert");
    await user.click(within(form).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: dictionaries.en["pull.editTickets"] }));
    expect(within(screen.getByRole("form", { name: dictionaries.en["pull.editTickets"] })).getByRole("checkbox")).toBeChecked();
  });

  it("shows a candidate load failure while allowing existing links to be removed", async () => {
    const tk = card("12", "ready");
    fakeApi(pullRoutes({
      [`GET ${P}`]: json(200, pull({ tickets: [{ url: tk.url, closes: false }] })),
      "GET /api/v1/workspaces/acme/board/cards": apiError(403, "forbidden", "Cannot list tickets."),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    await user.click(await screen.findByRole("button", { name: dictionaries.en["pull.editTickets"] }));
    const form = screen.getByRole("form", { name: dictionaries.en["pull.editTickets"] });
    expect(await within(form).findByRole("alert")).toHaveTextContent("Cannot list tickets.");
    expect(within(form).getByRole("button", { name: "Remove acme/demo#12" })).toBeEnabled();
  });

  describe.each(["en", "ja"] as const)("closed PR tickets (%s)", (language) => {
    it.each([
      { merged: false, linked: false }, { merged: false, linked: true },
      { merged: true, linked: false }, { merged: true, linked: true },
    ])("shows links but hides the empty section and editing (merged=$merged, linked=$linked)", async ({ merged, linked }) => {
      const words = dictionaries[language];
      const tk = card("12", "ready");
      const fake = fakeApi(pullRoutes({ [`GET ${P}`]: json(200, pull({ state: "closed", merged, tickets: linked ? [{ url: tk.url, closes: false }] : [] })) }));
      renderApp("/acme/demo/-/pulls/3", language);
      await screen.findByRole("heading", { name: /Fix login/ });
      expect(screen.queryByRole("button", { name: words["pull.editTickets"] })).not.toBeInTheDocument();
      const section = screen.queryByRole("region", { name: words["pull.tickets"] });
      if (linked) expect(within(section!).getByRole("link", { name: "acme/demo#12" })).toBeInTheDocument();
      else expect(section).not.toBeInTheDocument();
      expect(fake.calls("GET", "/api/v1/workspaces/acme/board/cards")).toHaveLength(0);
    });
  });
});

function pullRoutes(extra: Record<string, Parameters<typeof fakeApi>[0][string]> = {}) {
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [{ name: "acme", lanes: [] }], next_cursor: null }),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/members": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/repos/acme/demo": json(200, {
      path: "acme/demo",
      default_branch: "main",
      clone_url: "",
      url: "",
      protected_branches: ["main"],
      required_checks: ["test"],
      required_approvals: 0,
      revision: 1,
    }),
    [`GET ${P}`]: json(200, pull()),
    "GET /api/v1/repos/acme/demo/landing": json(200, { items: [pull().landing] }),
    [`GET ${P}/activity`]: json(200, {
      comments: [
        {
          id: "cmt_1",
          author: "aiko",
          body: "Workflow failed",
          meta: { routing: "suppress", kind: "workflow_error" },
          created_at: "2026-10-14T10:01:00.000Z",
          reactions: [],
          url: "",
        },
      ],
      reviews: [],
      threads: [],
    }),
    [`GET ${P}/threads`]: json(200, {
      items: [
        {
          id: "ic_1",
          resolved: false,
          outdated: false,
          path: "src/login.rs",
          line: 12,
          side: "RIGHT",
          commit: HEAD,
          comments: [{ id: "ic_1", author: "owner", body: "Why?", meta: null, created_at: "2026-10-14T10:02:00.000Z", reactions: [], url: "", reply_to_id: null }],
        },
      ],
      next_cursor: null,
    }),
    [`GET ${P}/commits`]: json(200, { items: [], next_cursor: null }),
    [`GET ${P}/files`]: json(200, {
      items: [{ path: "src/login.rs", previous_path: null, status: "modified", additions: 2, deletions: 1, patch: "@@ -1,2 +1,3 @@\n-old\n+new\n+more", patch_complete: true }],
      next_cursor: null,
    }),
    [`GET ${P}/checks`]: json(200, {
      head_sha: HEAD,
      base_sha: BASE,
      behind_by: 0,
      checks: [{ name: "test", status: "completed", conclusion: "failure", attempt_id: "att_1", attempt: 1 }],
      checks_expected: true,
      base_checks: 1,
    }),
    "GET /api/v1/repos/acme/demo/jobs/att_1": json(200, {
      id: "att_1",
      job: "test",
      attempt: 1,
      sha: HEAD,
      status: "completed",
      conclusion: "failure",
      started_at: null,
      finished_at: null,
      details: "exit code 101",
      artifact_names: [],
    }),
    "GET /api/v1/repos/acme/demo/jobs/att_1/log": json(200, { log: "test login ... FAILED", log_bytes: 90000, truncated: true }),
    ...extra,
  };
}

describe("pull request review", () => {
  // Each held source has a post earlier than every target, so it lands above the target when it arrives.
  it.each(["cmt_link", "rev_link", "ic_reply"].flatMap((id) => (["activity", "threads", "commits"] as const).map((held) => [id, held] as const)))(
    "opens and copies a permanent link to %s once the whole timeline has loaded (%s last), including a resolved thread's reply",
    async (id, held) => {
      const at = (minute: number) => `2026-10-14T10:0${minute}:00.000Z`;
      const comment = (id: string, minute = 3) => ({ id, author: "review-agent", body: id, meta: null, created_at: at(minute), reactions: [], url: "" });
      const activity = { comments: [comment("cmt_early", 0), comment("cmt_link")], reviews: [{
        id: "rev_link", author: "review-agent", body: "Review text", verdict: "approve", commit: HEAD, submitted_at: at(3), reactions: [],
      }], threads: [] };
      const threads = { items: [{
        id: "ic_first", resolved: true, outdated: true, path: "src/login.rs", line: 12, side: "RIGHT", commit: HEAD,
        comments: [comment("ic_first", 1), { ...comment("ic_reply"), reply_to_id: "ic_first" }],
      }], next_cursor: null };
      const commits = { items: [{ sha: HEAD, author: "review-agent", committed_at: at(2), subject: "Early commit" }], next_cursor: null };
      const sources = { activity, threads, commits };
      let answer!: (value: ReturnType<typeof json>) => void;
      const route = (source: keyof typeof sources) =>
        held === source ? () => new Promise<ReturnType<typeof json>>((resolve) => { answer = resolve; }) : json(200, sources[source]);
      fakeApi(pullRoutes({ [`GET ${P}/activity`]: route("activity"), [`GET ${P}/threads`]: route("threads"), [`GET ${P}/commits`]: route("commits") }));
      // Whether the whole timeline was there each time the target scrolled into view.
      const whole: boolean[] = [];
      vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (this: Element) {
        if (this.id === id) whole.push(["cmt_early", "ic_first", "cmt_link", "rev_link"].every((post) => document.getElementById(post)) && screen.queryByText("Early commit") !== null);
      });
      const user = userEvent.setup();
      const path = `/acme/demo/-/pulls/3#${id}`;
      const { client } = renderApp(path);
      for (const loaded of (["activity", "threads", "commits"] as const).filter((s) => s !== held)) {
        await waitFor(() => expect(client.getQueryData(["repo", "acme/demo", "pull", 3, loaded])).toBeDefined());
      }
      expect(document.getElementById(id)).not.toBeInTheDocument();
      await act(async () => answer(json(200, sources[held])));
      await waitFor(() => expect(document.getElementById(id)).toBeInTheDocument());
      const target = document.getElementById(id)!;
      await waitFor(() => expect(whole).not.toHaveLength(0));
      expect(whole).toEqual([true]);
      expect(target).toHaveAttribute("data-target", "true");
      expect(within(target).getByTitle("Link to this comment")).toHaveAttribute("href", path);
      await user.click(within(target).getByRole("button", { name: "Copy the comment's URL" }));
      expect(await navigator.clipboard.readText()).toBe(`${window.location.origin}${path}`);
      if (id === "ic_reply") {
        const thread = target.closest("details")!;
        expect(thread.open).toBe(true);
        await user.click(thread.querySelector("summary")!);
        await waitFor(() => expect(thread.open).toBe(false));
      }
    },
  );

  it.each(["activity", "threads", "commits"] as const)("shows the rest of the conversation and the error when %s fails to load", async (failed) => {
    fakeApi(pullRoutes({ [`GET ${P}/${failed}`]: apiError(404, "not_found", `No ${failed}.`) }));
    renderApp("/acme/demo/-/pulls/3");
    expect(await screen.findByText(`No ${failed}.`)).toBeInTheDocument();
    const timeline = screen.getByText(`No ${failed}.`).closest(".timeline") as HTMLElement;
    await waitFor(() => expect(within(timeline).queryByText("Loading…")).not.toBeInTheDocument());
    expect(within(timeline).queryByText("Workflow failed") !== null).toBe(failed !== "activity");
    expect(within(timeline).queryByText("Why?") !== null).toBe(failed !== "threads");
  });

  it("shows the description (or 'Not provided'), the files in a drawer, checks with reason and a log in a drawer, and status notices", async () => {
    const fake = fakeApi(pullRoutes());
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");

    expect(await screen.findByRole("heading", { name: /Fix login/ })).toBeInTheDocument();
    const description = screen.getByRole("region", { name: "Description" });
    expect(within(description).getByText("Not provided")).toBeInTheDocument();
    expect(await screen.findByLabelText("Status notice")).toHaveTextContent("Workflow failed");
    expect(screen.getByText("Why?")).toBeInTheDocument();

    // The changed files open in a drawer from the facts line; one file opens at once.
    await user.click(await screen.findByRole("button", { name: /^1 file\(s\)/ }));
    const files = await screen.findByRole("dialog", { name: /Changed files/ });
    expect(within(files).getByRole("link", { name: HEAD.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${HEAD}`);
    expect(within(files).getByRole("link", { name: BASE.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${BASE}`);
    expect(await within(files).findByText("+more")).toBeInTheDocument();
    await user.click(within(files).getByRole("button", { name: "Close" }));

    // A check row says why it failed and opens its job in a drawer.
    expect(await screen.findByText("Reason: exit code 101")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open check test" }));
    const check = await screen.findByRole("dialog", { name: /test/ });
    expect(await within(check).findByText("test login ... FAILED")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/jobs/att_1/log")[0]?.search).toBe("?tail_bytes=16384");
    expect(within(check).getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it.each([
    ["en", "3m 12s", "1m 5s · about 55s left", "1m 7s · about 53s left", "1m 0s · 30s last time"],
    ["ja", "3分12秒", "1分5秒 · 残り約 55秒", "1分7秒 · 残り約 53秒", "1分0秒 · 前回 30秒"],
  ] as const)("says how long each check ran, and while one runs how long so far and what is left (%s)", async (language, took, running, later, past) => {
    // The page's clock stands still but for the timers advanced below.
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date("2026-10-24T10:00:00Z"));
    const ago = (secs: number) => new Date(Date.now() - secs * 1000).toISOString();
    const times = { started_at: "2026-10-24T09:50:00Z", finished_at: "2026-10-24T09:53:12Z" };
    const unfinished = { conclusion: null, attempt: 1 } as const;
    fakeApi(
      pullRoutes({
        [`GET ${P}/checks`]: json(200, {
          ...CHECKS,
          checks: [
            { name: "test", status: "completed", conclusion: "failure", attempt_id: "att_1", attempt: 1, ...times },
            { name: "lint", status: "in_progress", attempt_id: "att_2", started_at: ago(65), expected_secs: 120, ...unfinished },
            { name: "web", status: "in_progress", attempt_id: "att_3", started_at: ago(60), expected_secs: 30, ...unfinished },
            { name: "fmt", status: "queued", attempt_id: "att_4", expected_secs: 30, ...unfinished },
          ],
        }),
        "GET /api/v1/repos/acme/demo/jobs/att_1": json(200, {
          id: "att_1", job: "test", attempt: 1, sha: HEAD, status: "completed", conclusion: "failure", details: "exit code 101", artifact_names: [], ...times,
        }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3", language);
    const words = dictionaries[language];
    const region = await screen.findByRole("region", { name: words["pull.checks"] });
    expect(await within(region).findByText(took)).toBeInTheDocument();
    // Past its estimate, a running check says what the job took before.
    expect(within(region).getByText(past)).toBeInTheDocument();
    // A running check counts each second.
    const lint = within(region).getByText(running);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(lint).toHaveTextContent(later);
    // A queued one has no time yet.
    expect(within(region).getByRole("button", { name: words["ci.open"].replace("{name}", "fmt") })).not.toHaveTextContent(/\d+(s|秒)/);
    // The job's drawer says how long it took.
    await user.click(within(region).getByRole("button", { name: words["ci.open"].replace("{name}", "test") }));
    const drawer = await screen.findByRole("dialog", { name: /test/ });
    expect(within(drawer).getByText(words["ci.duration"])).toBeInTheDocument();
    expect(within(drawer).getByText(took)).toBeInTheDocument();
  });

  it("attaches no images to a pull request's comments: only tickets keep them", async () => {
    const fake = fakeApi(pullRoutes());
    renderApp("/acme/demo/-/pulls/3");
    const field = await screen.findByRole("textbox", { name: "Write a comment or a review (Markdown)" });
    expect(screen.queryByRole("button", { name: "Attach images" })).not.toBeInTheDocument();
    fireEvent.paste(field, { clipboardData: { files: [new File(["x"], "shot.png", { type: "image/png" })], types: ["Files"] } });
    expect(field).toHaveValue("");
    expect(fake.requests.filter((r) => r.method === "POST" && r.path.endsWith("/attachments"))).toEqual([]);
  });

  it("opens the changed files at a thread's file from the thread", async () => {
    fakeApi(pullRoutes());
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    await user.click(await screen.findByRole("button", { name: "src/login.rs:12" }));
    const files = await screen.findByRole("dialog", { name: /Changed files/ });
    expect(await within(files).findByText("+more")).toBeInTheDocument();
  });

  it("places each thread at the time its first comment was posted and each commit at the time it was committed", async () => {
    const at = (minute: number) => `2026-10-14T10:0${minute}:00.000Z`;
    const post = (id: string, body: string, minute: number) => ({ id, author: "owner", body, meta: null, created_at: at(minute), reactions: [], url: "" });
    const thread = (id: string, line: number, minute: number) => ({
      id,
      resolved: false,
      outdated: false,
      path: "src/login.rs",
      line,
      side: "RIGHT",
      commit: HEAD,
      comments: [{ ...post(id, `Line ${line}?`, minute), reply_to_id: null }, { ...post(`${id}_reply`, "Later reply", 9), reply_to_id: id }],
    });
    fakeApi(
      pullRoutes({
        [`GET ${P}/activity`]: json(200, {
          comments: [post("cmt_1", "First comment", 1), post("cmt_2", "Last comment", 5)],
          reviews: [{ id: "rev_1", author: "aiko", verdict: "comment", body: "Review body", commit: HEAD, submitted_at: at(3), reactions: [] }],
          threads: [],
        }),
        [`GET ${P}/threads`]: json(200, { items: [thread("ic_2", 20, 4), thread("ic_1", 12, 2)], next_cursor: null }),
        [`GET ${P}/commits`]: json(200, {
          items: [
            { sha: BASE, author: "aiko", committed_at: at(0), subject: "Initial change" },
            { sha: HEAD, author: "aiko", committed_at: "2026-10-14T10:03:30.000Z", subject: "Address the review" },
          ],
          next_cursor: null,
        }),
      }),
    );
    renderApp("/acme/demo/-/pulls/3");
    const timeline = (await screen.findByText("Review body")).closest(".timeline") as HTMLElement;
    const order = ["Initial change", "First comment", "src/login.rs:12", "Review body", "Address the review", "src/login.rs:20", "Last comment"].map((text) => within(timeline).getByText(text));
    order.reduce((earlier, later) => {
      expect(earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
      return later;
    });
    const commit = within(timeline).getByText("Address the review").closest(".timeline-commit") as HTMLElement;
    expect(within(commit).getByText("aiko committed")).toBeInTheDocument();
    expect(within(commit).getByRole("link", { name: HEAD.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${HEAD}`);
  });

  it.each(["en", "ja"] as const)("sends the displayed head with a review and handles head_moved by reloading (%s)", async (language) => {
    const fake = fakeApi(
      pullRoutes({
        [`GET ${P}`]: [json(200, pull()), json(200, pull({ head_sha: NEW_HEAD }))],
        [`POST ${P}/reviews`]: apiError(409, "head_moved", "The head moved."),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3", language);
    await screen.findByRole("heading", { name: /Fix login/ });

    await user.click(screen.getByRole("button", { name: dictionaries[language]["verdict.approve"] }));

    const message = dictionaries[language]["pull.headMoved"].replace("{sha}", HEAD.slice(0, 10));
    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent(message);
    expect(within(refusal).getByRole("link", { name: HEAD.slice(0, 10) })).toHaveAttribute("href", `/acme/demo/-/commits/${HEAD}`);
    const review = fake.calls("POST", `${P}/reviews`)[0];
    expect(review?.body).toEqual({ verdict: "approve", body: "", commit: HEAD });
    expect(review?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
    // The pull request is reloaded and now shows the new head.
    await waitFor(() => expect(fake.calls("GET", P).length).toBeGreaterThanOrEqual(2));
    expect(await screen.findAllByTitle(NEW_HEAD)).not.toHaveLength(0);
  });

  it("shows each reviewer's state from the server on their avatar, and requests and withdraws reviews from the menu", async () => {
    const fake = fakeApi(
      pullRoutes({
        "GET /api/v1/members": json(200, {
          items: [
            { name: "owner", display_name: "Owner", kind: "owner", accounts: [], reviewers: [] },
            { name: "aiko", display_name: "Aiko", kind: "agent", accounts: [], reviewers: [] },
            { name: "ken", display_name: "Ken", kind: "agent", accounts: [], reviewers: [] },
            { name: "mia", display_name: "Mia", kind: "agent", accounts: [], reviewers: [] },
            { name: "ci", display_name: "CI", kind: "runner", accounts: [], reviewers: [] },
          ],
          next_cursor: null,
        }),
        [`GET ${P}`]: json(200, pull({ review_states: [
          { member: "ken", requested: false, verdict: "approve", outdated: false },
          { member: "mia", requested: false, verdict: "comment", outdated: true },
          { member: "owner", requested: true, verdict: null, outdated: false },
        ] })),
        [`PUT ${P}/reviewers/mia`]: json(200, {}),
        [`DELETE ${P}/reviewers/owner`]: json(200, {}),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    // An open request, the last verdict on the head, and a verdict on an older revision, as the
    // server decided them: the page reads no reviews for them.
    const trigger = await screen.findByRole("button", {
      name: "Reviewers: ken (Approved), mia (Commented (older revision)), owner (Requested) — click to request or withdraw",
    });
    expect(trigger.querySelector(".review-mark.approve:not(.stale)")).not.toBeNull();
    expect(trigger.querySelector(".review-mark.comment.stale")).not.toBeNull();
    expect(trigger.querySelector(".review-mark.pending")).not.toBeNull();
    await user.click(trigger);
    // The author and runners are not offered.
    expect(await screen.findByRole("menuitem", { name: /^owner/ })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitem", { name: /^ken/ })).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByRole("menuitem", { name: /^aiko/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^ci/ })).not.toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: /^mia/ }));
    await waitFor(() => expect(fake.calls("PUT", `${P}/reviewers/mia`)).toHaveLength(1));
    await user.click(screen.getByRole("menuitem", { name: /^owner/ }));
    await waitFor(() => expect(fake.calls("DELETE", `${P}/reviewers/owner`)).toHaveLength(1));
    expect(fake.calls("DELETE", `${P}/reviewers/owner`)[0]?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
  });

  it.each(["en", "ja"] as const)("uses a reviewer-specific empty label in the picker in %s", async (language) => {
    fakeApi(pullRoutes({ [`GET ${P}`]: json(200, pull({ review_states: [] })) }));
    renderApp("/acme/demo/-/pulls/3", language);
    expect(await screen.findByRole("button", { name: language === "ja" ? "レビュアー: なし — クリックで依頼を変更" : "Reviewers: no one — click to request or withdraw" })).toBeInTheDocument();
  });

  it("merges with the expected head and base and shows the refusal", async () => {
    const fake = fakeApi(
      pullRoutes({
        [`GET ${P}/checks`]: json(200, CHECKS),
        [`POST ${P}/merge`]: apiError(409, "not_mergeable", "Required check test did not succeed."),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    const merge = await screen.findByRole("button", { name: "Merge (fast-forward)" });
    await waitFor(() => expect(merge).toBeEnabled());
    await user.click(merge);

    expect(await screen.findByText("Not mergeable: Required check test did not succeed.")).toBeInTheDocument();
    expect(fake.calls("POST", `${P}/merge`)[0]?.body).toEqual({ expected_head: HEAD, expected_base: BASE });
  });

  const UPSTREAM_REPOSITORY = {
    path: "acme/demo",
    default_branch: "main",
    clone_url: "",
    url: "",
    protected_branches: ["main"],
    required_checks: ["test"],
    required_approvals: 0,
    auto_land: false,
    labels: [],
    upstream: {
      provider: "github",
      host: "GitHub",
      repository: "octo/demo",
      url: "https://github.com/octo/demo",
      reader: { member: "aiko", chosen: false },
      following: { tip: BASE, checked_at: null, followed_at: null, error: null, stopped: null },
    },
    revision: 1,
  };
  const UPSTREAM_REPO = json(200, UPSTREAM_REPOSITORY);
  const PREVIEW = {
    host: "GitHub",
    repository: "octo/demo",
    head: "ticket/3",
    base: "main",
    title: "Fix login",
    body: "Fixes the login.\n\nCloses #7",
    account: "aiko",
    login: "aiko-bot",
  };

  it.each(["en", "ja"] as const)("marks only required check names in the check list (%s)", async (language) => {
    fakeApi(pullRoutes({
      [`GET ${P}/checks`]: json(200, { ...CHECKS, checks: [SUCCESS, { ...SUCCESS, name: "test-optional" }] }),
    }));
    renderApp("/acme/demo/-/pulls/3", language);
    const checks = await screen.findByRole("region", { name: dictionaries[language]["pull.checks"] });
    const required = (await within(checks).findByText("test")).closest("li")!;
    const optional = within(checks).getByText("test-optional").closest("li")!;
    expect(within(required).getByText(dictionaries[language]["ci.required"])).toBeInTheDocument();
    expect(within(optional).queryByText(dictionaries[language]["ci.required"])).not.toBeInTheDocument();
  });

  for (const upstream of [false, true]) {
    const action = upstream ? "Publish to GitHub" : "Merge (fast-forward)";
    const repository = {
      ...UPSTREAM_REPOSITORY,
      upstream: upstream ? UPSTREAM_REPOSITORY.upstream : null,
    };
    const routes = {
      "GET /api/v1/repos/acme/demo": json(200, repository),
      [`GET ${P}/publish`]: json(200, PREVIEW),
    };

    it.each(["en", "ja"] as const)(`${action}: is enabled when no checks are required (%s)`, async (language) => {
      fakeApi(pullRoutes({
        ...routes,
        "GET /api/v1/repos/acme/demo": json(200, { ...repository, required_checks: [] }),
      }));
      renderApp("/acme/demo/-/pulls/3", language);
      // The default check fixture fails, but is optional in this repository.
      expect(await screen.findByText(`${dictionaries[language]["ci.reason"]}: exit code 101`)).toBeInTheDocument();
      const label = dictionaries[language][upstream ? "publish.submit" : "merge.submit"].replace("{host}", "GitHub");
      expect(screen.getByRole("button", { name: label })).toBeEnabled();
      expect(screen.queryByText(dictionaries[language]["ci.required"], { exact: true })).not.toBeInTheDocument();
    });

    const unsuccessful: { label: string; checks: Check[]; head_sha?: string }[] = [
      { label: "not registered", checks: [] },
      { label: "queued", checks: [{ ...SUCCESS, status: "queued", conclusion: null }] },
      { label: "running", checks: [{ ...SUCCESS, status: "in_progress", conclusion: null }] },
      ...(["failure", "cancelled", "timed_out", "skipped", "startup_failure", null] as const).map((conclusion) => ({
        label: `completed/${conclusion}`, checks: [{ ...SUCCESS, conclusion }],
      })),
      { label: "success for another head", checks: [SUCCESS], head_sha: NEW_HEAD },
    ];
    it.each(unsuccessful)(`${action}: blocks $label`, async ({ checks, head_sha }) => {
      const fake = fakeApi(pullRoutes({
        ...routes,
        "GET /api/v1/repos/acme/demo": json(200, { ...repository, required_checks: ["test", "lint"] }),
        [`GET ${P}/checks`]: json(200, { ...CHECKS, head_sha: head_sha ?? HEAD, checks: [...checks, { ...SUCCESS, name: "lint" }] }),
      }));
      const user = userEvent.setup();
      renderApp("/acme/demo/-/pulls/3");
      // Wait for the check read as well, so an initial loading state cannot satisfy this test.
      await within(await screen.findByRole("region", { name: "Checks" })).findByTitle(head_sha ?? HEAD);
      const button = screen.getByRole("button", { name: action });
      expect(button).toBeDisabled();
      await user.click(button);
      expect(fake.calls("POST", `${P}/${upstream ? "publish" : "merge"}`)).toHaveLength(0);
    });

    it(`${action}: stays disabled until checks load`, async () => {
      let answer!: (value: ReturnType<typeof json>) => void;
      fakeApi(pullRoutes({ ...routes, [`GET ${P}/checks`]: () => new Promise((resolve) => { answer = resolve; }) }));
      renderApp("/acme/demo/-/pulls/3");
      expect(await screen.findByRole("button", { name: action })).toBeDisabled();
      await waitFor(() => expect(answer).toBeTypeOf("function"));
      answer(json(200, CHECKS));
      await waitFor(() => expect(screen.getByRole("button", { name: action })).toBeEnabled());
    });

    it(`${action}: stays disabled when checks cannot be read`, async () => {
      fakeApi(pullRoutes({ ...routes, [`GET ${P}/checks`]: apiError(403, "forbidden", "Cannot read checks.") }));
      renderApp("/acme/demo/-/pulls/3");
      expect(await screen.findByText(/Cannot read checks\./)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: action })).toBeDisabled();
    });

    it(`${action}: enables on success after a check event and ignores optional failures`, async () => {
      fakeApi(pullRoutes({
        ...routes,
        "GET /api/v1/repos/acme/demo": json(200, { ...repository, required_checks: ["test", "lint"] }),
        [`GET ${P}/checks`]: [
          json(200, { ...CHECKS, checks: [] }),
          json(200, { ...CHECKS, checks: [SUCCESS, { ...SUCCESS, name: "lint" }, { ...SUCCESS, name: "optional", conclusion: "failure" }] }),
        ],
      }));
      const { client } = renderApp("/acme/demo/-/pulls/3");
      expect(await screen.findByRole("button", { name: action })).toBeDisabled();
      await screen.findByText("Checks are expected for this commit but none are registered yet.");
      applyChange({ kind: "check", path: "acme/demo", id: HEAD }, client);
      await waitFor(() => expect(screen.getByRole("button", { name: action })).toBeEnabled());
    });

    // Each filled green check is one approval; requested again or on an older head is not.
    const states = [
      { member: "ken", requested: false, verdict: "approve", outdated: false },
      { member: "mia", requested: false, verdict: "approve", outdated: true },
      { member: "li", requested: true, verdict: "approve", outdated: false },
      { member: "owner", requested: false, verdict: "request_changes", outdated: false },
    ] as const;
    it.each([
      { approvals: 1, required: 2, enabled: false },
      { approvals: 2, required: 2, enabled: true },
    ])(`${action}: is enabled only when $approvals / $required approvals suffice`, async ({ approvals, required, enabled }) => {
      const reviewStates = approvals === 1 ? [...states] : [...states, { ...states[0], member: "zoe" }];
      const fake = fakeApi(pullRoutes({
        ...routes,
        "GET /api/v1/repos/acme/demo": json(200, { ...repository, required_approvals: required }),
        [`GET ${P}/checks`]: json(200, CHECKS),
        [`GET ${P}`]: json(200, pull({ review_states: reviewStates, approvals })),
      }));
      const user = userEvent.setup();
      renderApp("/acme/demo/-/pulls/3");
      const trigger = await screen.findByRole("button", { name: /^Reviewers:/ });
      expect(trigger.querySelectorAll(".review-mark.approve:not(.stale)")).toHaveLength(approvals);
      const button = screen.getByRole("button", { name: action });
      await within(await screen.findByRole("region", { name: "Checks" })).findByTitle(HEAD);
      if (enabled) {
        expect(button).toBeEnabled();
        return;
      }
      expect(button).toBeDisabled();
      await user.click(button);
      expect(fake.calls("POST", `${P}/${upstream ? "publish" : "merge"}`)).toHaveLength(0);
    });
  }

  it("publishes to the upstream host as the author after showing exactly what is posted", async () => {
    const published = pull({
      upstream: {
        provider: "github",
        repository: "octo/demo",
        account: "aiko",
        number: 42,
        url: "https://github.com/octo/demo/pull/42",
        state: "open",
        pushed: HEAD,
        published_at: "2026-10-16T00:00:00Z",
        error: null,
        stopped: null,
        auto: false,
      },
    });
    const fake = fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}`]: [json(200, pull()), json(200, published)],
        [`GET ${P}/publish`]: json(200, PREVIEW),
        [`GET ${P}/checks`]: json(200, CHECKS),
        [`POST ${P}/publish`]: json(200, published),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    const preview = await screen.findByRole("figure", { name: "What will be posted" });
    expect(within(preview).getByText(/Closes #7/)).toBeInTheDocument();
    expect(within(preview).getByText("Posted on GitHub as aiko-bot.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Merge (fast-forward)" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Publish to GitHub" }));
    await waitFor(() => expect(fake.calls("POST", `${P}/publish`)).toHaveLength(1));
    expect(fake.calls("POST", `${P}/publish`)[0]?.body).toEqual({ expected_head: HEAD, expected_base: BASE });
    expect(await screen.findByRole("link", { name: "octo/demo#42" })).toHaveAttribute("href", "https://github.com/octo/demo/pull/42");
    expect(screen.getByText("Published. People review and merge it on GitHub; Branvil follows.")).toBeInTheDocument();
  });

  it("does not publish when the author has no account there, and says where to set it", async () => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}/publish`]: json(200, { ...PREVIEW, login: null }),
        [`GET ${P}/checks`]: json(200, CHECKS),
      }),
    );
    renderApp("/acme/demo/-/pulls/3");
    expect(
      await screen.findByText("aiko has no GitHub account in Branvil; set it under Members."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish to GitHub" })).toBeDisabled();
  });

  it("shows why sending to the host stopped and offers to resume landing", async () => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}`]: json(
          200,
          pull({
            upstream: {
              provider: "github",
              repository: "octo/demo",
              account: "aiko",
              number: 42,
              url: "https://github.com/octo/demo/pull/42",
              state: "open",
              pushed: BASE,
              published_at: "2026-10-16T00:00:00Z",
              error: null,
              stopped: "The host refused the branch ticket/3: [rejected] (stale info).",
              auto: true,
            },
          }),
        ),
        [`GET ${P}/publish`]: json(200, PREVIEW),
        [`GET ${P}/checks`]: json(200, CHECKS),
      }),
    );
    renderApp("/acme/demo/-/pulls/3");
    expect(await screen.findByRole("alert")).toHaveTextContent("Sending stopped: The host refused the branch ticket/3");
    expect(screen.getByRole("button", { name: "Resume landing" })).toBeEnabled();
    expect(screen.getByText("published automatically")).toBeInTheDocument();
  });

  it.each([
    { repository: { ...UPSTREAM_REPOSITORY, auto_land: true }, says: "This repository publishes it to GitHub by itself after it reaches the front of the queue and passes the gate." },
    { repository: { ...UPSTREAM_REPOSITORY, upstream: null, auto_land: true }, says: "This repository merges it by itself once it passes the gate at the front of the queue." },
  ])("says that a repository landing automatically acts by itself: $says", async ({ repository, says }) => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": json(200, repository),
        [`GET ${P}/publish`]: json(200, PREVIEW),
        [`GET ${P}/checks`]: json(200, CHECKS),
      }),
    );
    renderApp("/acme/demo/-/pulls/3");
    expect(await screen.findByText(says)).toBeInTheDocument();
  });

  it("says nothing of landing by itself when the repository does not", async () => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}/publish`]: json(200, PREVIEW),
        [`GET ${P}/checks`]: json(200, CHECKS),
      }),
    );
    renderApp("/acme/demo/-/pulls/3");
    expect(await screen.findByRole("button", { name: "Publish to GitHub" })).toBeInTheDocument();
    expect(screen.queryByText(/by itself/)).not.toBeInTheDocument();
    expect(screen.queryByText("published automatically")).not.toBeInTheDocument();
  });

  it("shows a check of the upstream host with its page there and its summary, without a Branvil job", async () => {
    const fake = fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}/publish`]: json(200, PREVIEW),
        [`GET ${P}/checks`]: json(200, {
          head_sha: HEAD,
          base_sha: BASE,
          behind_by: 0,
          checks: [
            {
              name: "github/test-windows (remainder)",
              status: "completed",
              conclusion: "failure",
              attempt_id: "github-41",
              details_url: "https://github.com/octo/demo/actions/runs/1/job/41",
              details: "test-windows (remainder): failure",
            },
          ],
          checks_expected: true,
          base_checks: 1,
        }),
        "GET /api/v1/repos/acme/demo/jobs/github-41/log": json(200, { log: "boom on Windows", log_bytes: 15, truncated: false }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    expect(await screen.findByText("Reason: test-windows (remainder): failure")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open check github/test-windows (remainder)" }));
    const drawer = await screen.findByRole("dialog", { name: /github\/test-windows/ });
    expect(within(drawer).getByRole("link", { name: "Open on the host" })).toHaveAttribute(
      "href",
      "https://github.com/octo/demo/actions/runs/1/job/41",
    );
    expect(await within(drawer).findByText("boom on Windows")).toBeInTheDocument();
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/jobs/github-41")).toHaveLength(0);
  });

  it("lets the owner edit the title and body in the publish panel, then shows the new preview", async () => {
    const edited = pull({ title: "Fix the login page", body: "Public words.", revision: 2 });
    const fake = fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo": UPSTREAM_REPO,
        [`GET ${P}`]: [json(200, pull()), json(200, edited)],
        [`GET ${P}/publish`]: [json(200, PREVIEW), json(200, { ...PREVIEW, title: "Fix the login page", body: "Public words." })],
        [`PATCH ${P}`]: json(200, edited),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3");
    await screen.findByRole("figure", { name: "What will be posted" });
    await user.click(screen.getByRole("button", { name: "Edit title and body" }));
    const form = screen.getByRole("form", { name: "Edit title and body" });
    expect(within(form).getByText(/They are Branvil's: GitHub gets them as previewed/)).toBeInTheDocument();
    await user.clear(within(form).getByRole("textbox", { name: "Title" }));
    await user.type(within(form).getByRole("textbox", { name: "Title" }), "Fix the login page");
    await user.clear(within(form).getByRole("textbox", { name: "Body" }));
    await user.type(within(form).getByRole("textbox", { name: "Body" }), "Public words.");
    await user.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fake.calls("PATCH", P)).toHaveLength(1));
    expect(fake.calls("PATCH", P)[0]?.body).toEqual({ title: "Fix the login page", body: "Public words.", revision: 1 });
    const preview = await screen.findByRole("figure", { name: "What will be posted" });
    expect(await within(preview).findByText("Public words.")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Edit title and body" })).not.toBeInTheDocument();
  });

  it("opens a job's address as its pull request with the job's drawer, and closing it leaves the job's address", async () => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [pull()], next_cursor: null }),
      }),
    );
    const user = userEvent.setup();
    renderApp("/acme/demo/-/jobs/att_1");
    expect(await screen.findByRole("heading", { name: /Fix login/ })).toBeInTheDocument();
    const drawer = await screen.findByRole("dialog", { name: /test/ });
    expect(await within(drawer).findByText("test login ... FAILED")).toBeInTheDocument();
    await user.click(within(drawer).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(window.location.pathname).toBe("/acme/demo/-/pulls/3"));
  });

  it("opens a job of a commit no open pull request has on the commit's page", async () => {
    fakeApi(
      pullRoutes({
        "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [], next_cursor: null }),
        [`GET /api/v1/repos/acme/demo/commits/${HEAD}`]: json(200, { sha: HEAD, message: "Try it", author: "aiko", authored_at: "2026-10-14T10:00:00.000Z", parents: [] }),
      }),
    );
    renderApp("/acme/demo/-/jobs/att_1");
    expect(await screen.findByRole("heading", { name: "Try it" })).toBeInTheDocument();
    expect(await within(await screen.findByRole("dialog", { name: /test/ })).findByText("test login ... FAILED")).toBeInTheDocument();
  });
});
