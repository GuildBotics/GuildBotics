import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { predicatesFor } from "../query/events";
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
  it.each(["pull", "repo", "upstream"] as const)("refreshes merged PR pages for a %s event in any listed repository", (kind) => {
    const lists = new QueryClient();
    const shown = keys.pullList(["acme/demo", "acme/web"], "closed");
    lists.setQueryData(shown, {});
    lists.setQueryData(keys.pullList(["beta/demo"], "all"), {});
    expect(stale(lists, { kind, path: "ACME/WEB", id: "1" })).toEqual([k(shown)]);
  });
  const origin = window.location.origin;
  const client = new QueryClient();
  client.setQueryData(keys.board("acme"), { name: "acme", lanes: [] });
  client.setQueryData(keys.cards("acme", "open"), {
    items: [{ repository: "acme/demo", key: "1", pull_requests: [{ url: `${origin}/acme/demo/-/pulls/3` }] }],
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
        k(keys.cards("acme", "open")),
        k(keys.ticket("acme/demo", "1")),
        k(keys.ticketPart("acme/demo", "1", "comments")),
        k(keys.ticketPart("acme/demo", "1", "pulls")),
      ].sort(),
    );
    expect(stale(client, { kind: "ticket", path: "acme/other", id: "1" })).toEqual(
      [k(keys.cards("acme", "open")), k(keys.ticket("acme/other", "1"))].sort(),
    );
  });

  it("maps a pull change to that pull request only (not its diff) and to cards linking it", () => {
    expect(stale(client, { kind: "pull", path: "acme/demo", id: "3" })).toEqual(
      [
        k(keys.cards("acme", "open")),
        k(keys.pull("acme/demo", 3)),
        k(keys.pullPart("acme/demo", 3, "checks")),
        k(keys.ticketPart("acme/demo", "1", "pulls")),
      ].sort(),
    );
    expect(stale(client, { kind: "pull", path: "acme/demo", id: "4" })).toEqual(
      [k(keys.pull("acme/demo", 4)), k(keys.pullPart("acme/demo", 4, "checks")), k(keys.ticketPart("acme/demo", "1", "pulls"))].sort(),
    );
  });

  it("maps a check change to the checks of the pull request whose head is that commit", () => {
    expect(stale(client, { kind: "check", path: "acme/demo", id: "bbb" })).toEqual([k(keys.pullPart("acme/demo", 4, "checks"))]);
  });

  it("maps a check change to the checks of that commit, not to the commit itself", () => {
    expect(stale(client, { kind: "check", path: "acme/demo", id: "ccc" })).toEqual([k(keys.commitChecks("acme/demo", "ccc"))]);
  });

  it("maps a check change of a commit whose checks are still loading by the sha it was asked for", () => {
    const loading = new QueryClient();
    void loading.prefetchQuery({ queryKey: keys.commitChecks("acme/demo", "ddd"), queryFn: () => new Promise(() => {}) });
    expect(stale(loading, { kind: "check", path: "acme/demo", id: "ddd" })).toEqual([k(keys.commitChecks("acme/demo", "ddd"))]);
    expect(stale(loading, { kind: "check", path: "acme/demo", id: "eee" })).toEqual([]);
  });

  it("refreshes a pull's gate checks by the gate commit and its queue when the leader changes", () => {
    const queue = new QueryClient();
    queue.setQueryData(keys.landing("acme/demo"), { items: [] });
    queue.setQueryData(keys.pull("acme/demo", 4), { head_sha: "head", landing: { gate: "gate", first: { number: 3 } } });
    queue.setQueryData(keys.pullPart("acme/demo", 4, "checks"), { head_sha: "head" });
    expect(stale(queue, { kind: "check", path: "acme/demo", id: "gate" })).toEqual([k(keys.pullPart("acme/demo", 4, "checks"))]);
    expect(stale(queue, { kind: "pull", path: "acme/demo", id: "3" })).toEqual([k(keys.landing("acme/demo")), k(keys.pull("acme/demo", 4))].sort());
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
