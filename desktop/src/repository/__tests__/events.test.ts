import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { applyChange, predicatesFor, refresh } from "../query/events";
import { keys } from "../query/keys";

function stale(client: QueryClient, event: Parameters<typeof predicatesFor>[0]) {
  const predicates = predicatesFor(event, client);
  return client
    .getQueryCache()
    .getAll()
    .filter((q) => predicates === null || predicates.some((p) => p(q)))
    .map((q) => JSON.stringify(q.queryKey))
    .sort();
}

const k = (key: readonly unknown[]) => JSON.stringify(key);

describe("change events", () => {
  it.each(["pull", "check", "repo", "upstream"] as const)(
    "refreshes merged PR pages for a %s event in any listed repository",
    (kind) => {
      const lists = new QueryClient();
      const shown = keys.pullList(["acme/demo", "acme/web"], "closed");
      lists.setQueryData(shown, {});
      lists.setQueryData(keys.pullList(["beta/demo"], "all"), {});
      expect(stale(lists, { kind, path: "ACME/WEB", id: "1" })).toEqual([k(shown)]);
    },
  );
  const origin = window.location.origin;
  const client = new QueryClient();
  client.setQueryData(keys.board("acme"), { name: "acme", lanes: [] });
  client.setQueryData(keys.cards("acme", "open"), {
    items: [
      {
        repository: "acme/demo",
        key: "1",
        pull_requests: [{ url: `${origin}/workspaces/acme/repos/demo/pulls/3` }],
      },
    ],
  });
  client.setQueryData(keys.cards("beta", "open"), { items: [] });
  client.setQueryData(keys.ticket("acme/demo", "1"), {});
  client.setQueryData(keys.ticketPart("acme/demo", "1", "comments"), {});
  client.setQueryData(keys.ticketPart("acme/demo", "1", "pulls"), {});
  client.setQueryData(keys.ticket("acme/demo", "2"), {});
  client.setQueryData(keys.ticket("acme/other", "1"), {});
  client.setQueryData(keys.pull("acme/demo", 3), { head_sha: "aaa" });
  client.setQueryData([...keys.pullPart("acme/demo", 3, "files"), "aaa"], {});
  client.setQueryData(keys.pullPart("acme/demo", 3, "checks"), { head_sha: "aaa" });
  client.setQueryData(keys.pull("acme/demo", 4), { head_sha: "bbb" });
  client.setQueryData(keys.pullPart("acme/demo", 4, "checks"), { head_sha: "bbb" });
  client.setQueryData(keys.commit("acme/demo", "aaa"), {});
  client.setQueryData(keys.commit("acme/demo", "ccc"), { sha: "ccc" });
  client.setQueryData(keys.commitChecks("acme/demo", "ccc"), { sha: "ccc" });
  client.setQueryData(keys.branch("acme/demo", "main"), { name: "main", sha: "ccc" });
  client.setQueryData(keys.repo("acme/other"), {});

  it("maps a ticket change to that ticket of that repository and the cards of its workspace's board", () => {
    // The board may show the ticket even when it shows no card of its repository yet.
    expect(stale(client, { kind: "ticket", path: "acme/demo", id: "1" })).toEqual(
      [
        ...checkKeys,
        k(keys.cards("acme", "open")),
        k(keys.ticket("acme/demo", "1")),
        k(keys.ticketPart("acme/demo", "1", "comments")),
        k(keys.ticketPart("acme/demo", "1", "pulls")),
      ].sort(),
    );
    expect(stale(client, { kind: "ticket", path: "acme/other", id: "1" })).toEqual(
      [...checkKeys, k(keys.cards("acme", "open")), k(keys.ticket("acme/other", "1"))].sort(),
    );
  });

  const checkKeys = [
    k(keys.pullPart("acme/demo", 3, "checks")),
    k(keys.pullPart("acme/demo", 4, "checks")),
    k(keys.commitChecks("acme/demo", "ccc")),
  ];

  it("maps a pull change to its page and linked cards, and refreshes queued-check ordering", () => {
    expect(stale(client, { kind: "pull", path: "acme/demo", id: "3" })).toEqual(
      [
        k(keys.cards("acme", "open")),
        k(keys.pull("acme/demo", 3)),
        ...checkKeys,
        k(keys.ticketPart("acme/demo", "1", "pulls")),
      ].sort(),
    );
    expect(stale(client, { kind: "pull", path: "acme/demo", id: "4" })).toEqual(
      [
        k(keys.pull("acme/demo", 4)),
        ...checkKeys,
        k(keys.ticketPart("acme/demo", "1", "pulls")),
      ].sort(),
    );
  });

  it("refreshes all check lists when a completion can change the queued order", () => {
    expect(stale(client, { kind: "check", path: "acme/demo", id: "bbb" })).toEqual(
      [...checkKeys, k(keys.pull("acme/demo", 4))].sort(),
    );
    expect(stale(client, { kind: "pull", path: "other/repo", id: "1" })).toEqual(
      [...checkKeys, k(keys.ticketPart("acme/demo", "1", "pulls"))].sort(),
    );
  });

  it("maps a check change to the checks of that commit, not to the commit itself", () => {
    expect(stale(client, { kind: "check", path: "acme/demo", id: "ccc" })).toEqual(
      [...checkKeys].sort(),
    );
  });

  it("maps a check change of a commit whose checks are still loading by the sha it was asked for", () => {
    const loading = new QueryClient();
    void loading.prefetchQuery({
      queryKey: keys.commitChecks("acme/demo", "ddd"),
      queryFn: () => new Promise(() => {}),
    });
    expect(stale(loading, { kind: "check", path: "acme/demo", id: "ddd" })).toEqual([
      k(keys.commitChecks("acme/demo", "ddd")),
    ]);
    expect(stale(loading, { kind: "check", path: "acme/demo", id: "eee" })).toEqual([
      k(keys.commitChecks("acme/demo", "ddd")),
    ]);
  });

  it("maps a check change to the attempts of a job of that commit, known by the job they were read for", () => {
    const jobs = new QueryClient();
    jobs.setQueryData(keys.job("acme/demo", "att_1"), { sha: "aaa" });
    jobs.setQueryData(keys.jobAttempts("acme/demo", "att_1"), []);
    jobs.setQueryData(keys.jobAttempts("acme/demo", "att_2"), []);
    jobs.setQueryData(keys.jobLog("acme/demo", "att_1", 16384), {});
    expect(stale(jobs, { kind: "check", path: "acme/demo", id: "aaa" })).toEqual(
      [k(keys.job("acme/demo", "att_1")), k(keys.jobAttempts("acme/demo", "att_1"))].sort(),
    );
    expect(stale(jobs, { kind: "check", path: "acme/demo", id: "bbb" })).toEqual([]);
  });

  it("matches the queries known by their own answer while that first answer is on its way", () => {
    const loading = new QueryClient();
    const pending = (key: readonly unknown[]) =>
      void loading.prefetchQuery({ queryKey: key, queryFn: () => new Promise(() => {}) });
    pending(keys.job("acme/demo", "att_1"));
    pending(keys.jobLog("acme/demo", "att_1", 16384));
    pending(keys.pull("acme/demo", 3));
    pending(keys.pullPart("acme/demo", 3, "checks"));
    pending(keys.pull("acme/other", 4));
    expect(stale(loading, { kind: "check", path: "acme/demo", id: "aaa" })).toEqual(
      [
        k(keys.job("acme/demo", "att_1")),
        k(keys.pull("acme/demo", 3)),
        k(keys.pullPart("acme/demo", 3, "checks")),
      ].sort(),
    );
    // A pull request's tickets and landing blockers are not known yet either.
    expect(stale(loading, { kind: "ticket", path: "acme/tickets", id: "1" })).toEqual(
      [
        k(keys.pull("acme/demo", 3)),
        k(keys.pullPart("acme/demo", 3, "checks")),
        k(keys.pull("acme/other", 4)),
      ].sort(),
    );
    // Nor which pull requests a board's cards link.
    pending(keys.cards("acme", "open"));
    expect(stale(loading, { kind: "pull", path: "acme/demo", id: "3" })).toContain(
      k(keys.cards("acme", "open")),
    );
  });

  it("does not match a query whose fetch failed before it had an answer", async () => {
    const failed = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    for (const key of [keys.job("acme/demo", "att_1"), keys.pull("acme/demo", 3)])
      await failed.prefetchQuery({ queryKey: key, queryFn: () => Promise.reject(new Error("x")) });
    expect(stale(failed, { kind: "check", path: "acme/demo", id: "aaa" })).toEqual([]);
  });

  it("refreshes ticket-linked landing decisions across repositories and ancestors", () => {
    const c = new QueryClient();
    c.setQueryData(keys.pull("acme/demo", 3), {
      tickets: [{ url: "http://localhost/workspaces/acme/repos/tickets/tickets/1" }],
    });
    c.setQueryData(keys.pull("beta/demo", 4), {
      tickets: [{ url: "http://localhost/workspaces/beta/repos/tickets/tickets/2" }],
    });
    c.setQueryData(keys.pull("acme/demo", 5), { tickets: [] });
    // A ticket event can name an ancestor, which is not part of the PR cache.
    expect(stale(c, { kind: "ticket", path: "acme/tickets", id: "99" })).toEqual([
      k(keys.pull("acme/demo", 3)),
      k(keys.pull("beta/demo", 4)),
    ]);
  });

  it("refreshes landing decisions for pulls sharing the checked head", () => {
    const c = new QueryClient();
    c.setQueryData(keys.pull("acme/demo", 3), { head_sha: "head" });
    c.setQueryData(keys.pull("acme/demo", 4), { head_sha: "head" });
    c.setQueryData(keys.pull("acme/demo", 5), { head_sha: "other" });
    expect(stale(c, { kind: "check", path: "acme/demo", id: "head" })).toEqual([
      k(keys.pull("acme/demo", 3)),
      k(keys.pull("acme/demo", 4)),
    ]);
  });

  it("refreshes a pull independently of another pull's state", () => {
    const queue = new QueryClient();
    queue.setQueryData(keys.pull("acme/demo", 4), {
      head_sha: "head",
      landing: { next: "conditions", blockers: [] },
    });
    queue.setQueryData(keys.pullPart("acme/demo", 4, "checks"), { head_sha: "head", checks: [] });
    expect(stale(queue, { kind: "pull", path: "acme/demo", id: "3" })).toEqual([
      k(keys.pullPart("acme/demo", 4, "checks")),
    ]);
  });

  it("maps a repository change (a push, new settings) to its pull requests, branch tips, and commits' checks but not diffs, commits, or tickets", () => {
    expect(stale(client, { kind: "repo", path: "acme/demo", id: "" })).toEqual(
      [
        k(keys.branch("acme/demo", "main")),
        k(keys.commitChecks("acme/demo", "ccc")),
        k(keys.pull("acme/demo", 3)),
        k(keys.pullPart("acme/demo", 3, "checks")),
        k(keys.pull("acme/demo", 4)),
        k(keys.pullPart("acme/demo", 4, "checks")),
      ].sort(),
    );
  });

  it("maps a board change to the board of that workspace", () => {
    expect(stale(client, { kind: "board", path: "acme", id: "" })).toEqual(
      [k(keys.board("acme")), k(keys.cards("acme", "open"))].sort(),
    );
  });

  it("matches paths whatever their case, as the service does", () => {
    // A URL typed as /ACME or /Acme/Demo opens the same items as /acme and /acme/demo.
    const typed = new QueryClient();
    typed.setQueryData(keys.board("ACME"), { name: "acme", lanes: [] });
    typed.setQueryData(keys.cards("ACME", "open"), { items: [] });
    typed.setQueryData(keys.ticket("Acme/Demo", "1"), {});
    expect(stale(typed, { kind: "ticket", path: "acme/demo", id: "1" })).toEqual(
      [k(keys.cards("ACME", "open")), k(keys.ticket("Acme/Demo", "1"))].sort(),
    );
    expect(stale(typed, { kind: "board", path: "acme", id: "" })).toEqual(
      [k(keys.board("ACME")), k(keys.cards("ACME", "open"))].sort(),
    );
  });

  it("refetches everything for an event without a path", () => {
    expect(predicatesFor({ kind: "ticket" }, client)).toBeNull();
  });
});

describe("refresh", () => {
  /** A query whose running fetch answers on `release`; later answers come at once. */
  function held(client: QueryClient, { shown, data }: { shown: boolean; data?: string }) {
    let release!: (value: string) => void;
    let calls = 0;
    const options = {
      queryKey: ["held"],
      queryFn: () => {
        calls += 1;
        return calls === 1
          ? new Promise<string>((resolve) => {
              release = resolve;
            })
          : Promise.resolve(`answer ${calls}`);
      },
    };
    if (data !== undefined) client.setQueryData(["held"], data, { updatedAt: 0 });
    const stop = shown
      ? new QueryObserver(client, options).subscribe(() => {})
      : (void client.prefetchQuery(options), () => {});
    return { release: (v: string) => release(v), calls: () => calls, stop };
  }

  it.each([
    ["first fetch", undefined],
    ["refetch", "earlier"],
  ])(
    "asks a shown query once more after its running %s, which may predate the changes, however many arrived",
    async (_, data) => {
      const client = new QueryClient();
      const query = held(client, { shown: true, data });
      await vi.waitFor(() => expect(query.calls()).toBe(1));
      for (let i = 0; i < 3; i++) refresh(client, { queryKey: ["held"] });
      // The running fetch is not replaced: its request would go on anyway.
      expect(query.calls()).toBe(1);
      query.release("before the changes");
      await vi.waitFor(() => expect(client.getQueryData(["held"])).toBe("answer 2"));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(query.calls()).toBe(2);
      query.stop();
    },
  );

  it("keeps an awaited refetch of that query waiting for its answer", async () => {
    const client = new QueryClient();
    const query = held(client, { shown: true });
    await vi.waitFor(() => expect(query.calls()).toBe(1));
    let done = false;
    void client.refetchQueries({ queryKey: ["held"] }).then(() => {
      done = true;
    });
    refresh(client, { queryKey: ["held"] });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(done).toBe(false);
    query.release("first");
    await vi.waitFor(() => expect(done).toBe(true));
    query.stop();
  });

  it.each([
    ["first fetch", undefined],
    ["refetch", "earlier"],
  ])(
    "leaves a hidden query's running %s to finish and keeps it stale after that",
    async (_, data) => {
      const client = new QueryClient();
      const query = held(client, { shown: false, data });
      await vi.waitFor(() => expect(query.calls()).toBe(1));
      refresh(client, { queryKey: ["held"] });
      query.release("answered");
      await vi.waitFor(() => expect(client.getQueryData(["held"])).toBe("answered"));
      await vi.waitFor(() => expect(client.getQueryState(["held"])?.isInvalidated).toBe(true));
      expect(query.calls()).toBe(1);
    },
  );
});

describe("refresh after the fetch it waited for", () => {
  it("leaves the next read to whoever cancelled that fetch, so an optimistic change stays", async () => {
    let calls = 0;
    const client = new QueryClient();
    client.setQueryData(["settings"], "saved", { updatedAt: 0 });
    const stop = new QueryObserver(client, {
      queryKey: ["settings"],
      queryFn: () => {
        calls += 1;
        return new Promise<string>(() => {});
      },
    }).subscribe(() => {});
    await vi.waitFor(() => expect(calls).toBe(1));
    refresh(client, { queryKey: ["settings"] });
    // A save cancels the running read and shows its change before it is sent.
    void client.cancelQueries({ queryKey: ["settings"] });
    client.setQueryData(["settings"], "changed");
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(client.getQueryData(["settings"])).toBe("changed");
    expect(client.getQueryState(["settings"])?.isInvalidated).toBe(true);
    expect(calls).toBe(1);
    stop();
  });

  it("reads Markdown once for an event that refreshes everything", async () => {
    let calls = 0;
    const client = new QueryClient();
    const stop = new QueryObserver(client, {
      queryKey: ["markdown", "x"],
      queryFn: () => Promise.resolve(++calls),
    }).subscribe(() => {});
    await vi.waitFor(() => expect(calls).toBe(1));
    applyChange({ kind: "ticket" }, client);
    await vi.waitFor(() => expect(calls).toBe(2));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(calls).toBe(2);
    stop();
  });
});
