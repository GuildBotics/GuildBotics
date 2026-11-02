import type { Card, Landing, PullRequest } from "../api/types";

/** A card of ticket `key` of `repository` (acme/demo unless `extra` names another). */
export function card(key: string, lane: Card["lane"], extra: Partial<Card> = {}): Card {
  const repository = extra.repository ?? "acme/demo";
  return {
    url: `${window.location.origin}/${repository}/-/tickets/${key}`,
    repository,
    key,
    title: `Title of ${key}`,
    body: "",
    state: "open",
    state_reason: null,
    author: "aiko",
    assignee: null,
    labels: [],
    interactive: false,
    interactive_ancestor: null,
    upstream_issue: null,
    lane,
    rank: "0|a",
    parent: null,
    children: [],
    blocked_by: [],
    blocks: [],
    created_at: "2026-10-14T10:00:00.000Z",
    updated_at: "2026-10-14T10:00:00.000Z",
    closed_at: null,
    revision: 1,
    comments: [],
    pull_requests: [],
    ...extra,
  };
}

export const HEAD = "a".repeat(40);
export const NEW_HEAD = "b".repeat(40);
export const BASE = "c".repeat(40);

export function landing(extra: Partial<Landing> = {}): Landing {
  const state = extra.state ?? "passed";
  return {
    number: 3, base: "main", queued_at: "2026-10-14T10:00:00Z", resume_publish: false, title: "Fix login", pull_state: "open", draft: false, error: null,
    state: "passed", state_at: "2026-10-14T10:01:00Z", gate: HEAD,
    base_sha: BASE, head_sha: HEAD, reason: null, position: 1,
    first: { number: 3, next: state === "passed" ? "owner_merge_host" : "gate", host_url: "https://example.test/pulls/3", next_detail: null, state, state_at: "2026-10-14T10:01:00Z", error: null },
    ...extra,
  };
}

export function pull(extra: Partial<PullRequest> = {}): PullRequest {
  return {
    url: `${window.location.origin}/acme/demo/-/pulls/3`,
    repository: "acme/demo",
    number: 3,
    label: "!3",
    title: "Fix login",
    body: "",
    state: "open",
    merged: false,
    draft: false,
    author: "aiko",
    head: "ticket/3",
    head_repository: "acme/demo",
    head_sha: HEAD,
    base: "main",
    base_sha: BASE,
    reviewers: ["owner"],
    review_states: [{ member: "owner", requested: true, verdict: null, outdated: false }],
    approvals: 0,
    unanswered: [],
    tickets: [],
    labels: [],
    changed_files: 1,
    ready_at: null,
    created_at: "2026-10-14T10:00:00.000Z",
    updated_at: "2026-10-14T10:00:00.000Z",
    closed_at: null,
    merged_at: null,
    merge_sha: null,
    upstream: null,
    landing: landing(),
    revision: 1,
    ...extra,
  };
}
