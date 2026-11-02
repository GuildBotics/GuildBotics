// Typed calls of the Branvil HTTP API v1 used by the GUI (docs/api.md).

import { enc, get, getAll, newIdempotencyKey, request } from "./client";
import type {
  AccountCredential,
  Activity,
  Attachment,
  Branch,
  Card,
  Checks,
  CiLimits,
  CiSettings,
  Comment,
  Commit,
  CommitChecks,
  Diagnostics,
  FileChange,
  IssuedToken,
  Job,
  JobLog,
  Lane,
  Landing,
  Member,
  MemberKind,
  Page,
  PullCommit,
  PullRequest,
  PublishPreview,
  PullSummary,
  Reaction,
  Repository,
  Review,
  Session,
  Thread,
  ThreadComment,
  Ticket,
  TicketLink,
  TokenInfo,
  Verdict,
  Webhook,
  Workspace,
} from "./types";

const create = <T>(path: string, body: unknown) =>
  request<T>("POST", path, { body, idempotencyKey: newIdempotencyKey() });

/** Read-only resolution; no idempotency record should cache target states. */
export const markdown = (repo: string, source: string, signal?: AbortSignal) =>
  request<{ markdown: string }>("POST", `/repos/${enc(repo)}/markdown`, { body: { source }, signal });

// Session -------------------------------------------------------------------

export const session = {
  get: () => get<Session>("/session"),
  /** Exchanges the code of a `branvil open` link (once) for the session cookie. */
  enter: (code: string) => request<Session>("POST", "/session", { body: { code } }),
};

// Members (owner) -----------------------------------------------------------

export const members = {
  list: () => getAll<Member>("/members"),
  create: (body: { name: string; display_name: string; kind: Exclude<MemberKind, "owner"> }) =>
    create<Member>("/members", body),
  update: (name: string, body: { display_name: string }) =>
    request<Member>("PATCH", `/members/${enc(name)}`, { body }),
  setAvatar: (name: string, image: Blob) => request<Member>("PUT", `/members/${enc(name)}/avatar`, { body: image }),
  removeAvatar: (name: string) => request<Member>("DELETE", `/members/${enc(name)}/avatar`),
  setReviewer: (name: string, reviewer: string, on: boolean) =>
    request<Member>(on ? "PUT" : "DELETE", `/members/${enc(name)}/reviewers/${enc(reviewer)}`),
  tokens: (name: string) => getAll<TokenInfo>(`/members/${enc(name)}/tokens`),
  issueToken: (name: string, label: string) => create<IssuedToken>(`/members/${enc(name)}/tokens`, { label }),
  revokeToken: (name: string, id: string) => request<void>("DELETE", `/members/${enc(name)}/tokens/${enc(id)}`),
  /** Sets a member's account on an upstream host; the host checks the token first. */
  setAccount: (name: string, provider: string, credential: AccountCredential) =>
    request<Member>("PUT", `/members/${enc(name)}/accounts/${enc(provider)}`, { body: credential }),
  deleteAccount: (name: string, provider: string) =>
    request<Member>("DELETE", `/members/${enc(name)}/accounts/${enc(provider)}`),
};

export const me = () => get<Member>("/me");

// Local apps ------------------------------------------------------------------

export const apps = {
  /** The owner allows a local app; the answer is where to send the browser with its code. */
  authorize: (body: { client: string; redirect_uri: string; state: string; code_challenge: string }) =>
    request<{ redirect: string }>("POST", "/apps/authorizations", { body }),
};

// Repositories ---------------------------------------------------------------

export interface RepoPatch {
  revision: number;
  default_branch?: string;
  required_checks?: string[];
  required_approvals?: number;
  auto_land?: boolean;
  ci_network?: boolean;
  protected_branches?: string[];
  labels?: string[];
}

export const repos = {
  list: () => getAll<Repository>("/repos"),
  get: (repo: string) => get<Repository>(`/repos/${enc(repo)}`),
  create: (body: { path: string; default_branch: string; labels: string[] }) => create<Repository>("/repos", body),
  update: (repo: string, body: RepoPatch) => request<Repository>("PATCH", `/repos/${enc(repo)}`, { body }),
  pulls: (repo: string, state: "open" | "closed" | "all", cursor?: string | null) =>
    get<Page<PullRequest>>(`/repos/${enc(repo)}/pulls`, { state, cursor, limit: 30 }),
  landing: (repo: string) => get<{ items: Landing[] }>(`/repos/${enc(repo)}/landing`),
  branch: (repo: string, name: string) => get<Branch>(`/repos/${enc(repo)}/branches/${enc(name)}`),
  commit: (repo: string, sha: string) => get<Commit>(`/repos/${enc(repo)}/commits/${enc(sha)}`),
  commitChecks: (repo: string, sha: string) => get<CommitChecks>(`/repos/${enc(repo)}/commits/${enc(sha)}/checks`),
  /** `reader`: a member names it, `null` picks again, absent keeps it. */
  setUpstream: (repo: string, body: { provider: string; repository: string; reader?: string | null }) =>
    request<Repository>("PUT", `/repos/${enc(repo)}/upstream`, { body }),
  deleteUpstream: (repo: string) => request<Repository>("DELETE", `/repos/${enc(repo)}/upstream`),
  /** Polls the upstream now and answers with what it found. */
  refreshUpstream: (repo: string) => request<Repository>("POST", `/repos/${enc(repo)}/upstream/refresh`),
};

// Tickets (under their repository) -------------------------------------------

export interface TicketPatch {
  title?: string;
  body?: string;
  add_labels?: string[];
  remove_labels?: string[];
  interactive?: boolean;
  state?: "open" | "closed";
  state_reason?: "completed" | "not_planned" | null;
  upstream_issue?: string | null;
  /** Ticket URLs: the parent (null for none), tickets it is blocked by. */
  parent?: string | null;
  add_blocked_by?: string[];
  remove_blocked_by?: string[];
  revision?: number;
}

const ticketPath = (repo: string, key: string) => `/repos/${enc(repo)}/tickets/${enc(key)}`;

export const tickets = {
  /** The ticket goes last in `lane` of its workspace's board (`new` without one). */
  create: (repo: string, body: { title: string; body: string; labels: string[]; lane?: Lane }) =>
    create<Ticket>(`/repos/${enc(repo)}/tickets`, body),
  get: (repo: string, key: string) => get<Ticket>(ticketPath(repo, key)),
  delete: (repo: string, key: string) => request<void>("DELETE", ticketPath(repo, key)),
  update: (repo: string, key: string, body: TicketPatch) =>
    request<{ ticket: Ticket; state_changed: boolean }>("PATCH", ticketPath(repo, key), { body }),
  comments: (repo: string, key: string) => getAll<Comment>(`${ticketPath(repo, key)}/comments`),
  addComment: (repo: string, key: string, body: string) =>
    create<Comment>(`${ticketPath(repo, key)}/comments`, { body }),
  editComment: (repo: string, key: string, id: string, body: string) =>
    request<Comment>("PATCH", `${ticketPath(repo, key)}/comments/${enc(id)}`, { body: { body } }),
  deleteComment: (repo: string, key: string, id: string) =>
    request<void>("DELETE", `${ticketPath(repo, key)}/comments/${enc(id)}`),
  react: (repo: string, key: string, targetId: string, reaction: Reaction, on: boolean) =>
    request<unknown>(on ? "POST" : "DELETE", `${ticketPath(repo, key)}/reactions`, {
      body: { target_id: targetId, reaction },
    }),
  pulls: (repo: string, key: string) => getAll<PullSummary>(`${ticketPath(repo, key)}/pulls`),
  /** Uploads an image for a ticket's body or comments to show by its `url`. */
  attach: (repo: string, image: Blob, name: string) =>
    request<Attachment>("POST", `/repos/${enc(repo)}/attachments`, { body: image, query: { name } }),
  assign: (repo: string, key: string, name: string) =>
    request<Ticket>("PUT", `${ticketPath(repo, key)}/assignee`, { body: { name } }),
  unassign: (repo: string, key: string) => request<Ticket>("DELETE", `${ticketPath(repo, key)}/assignee`),
  /** Moves the ticket to a lane of its workspace's board; `before`/`after` name a card of the lane by its `ticketRef`. */
  place: (repo: string, key: string, body: { lane: Lane; before?: string; after?: string }) =>
    request<unknown>("PUT", `${ticketPath(repo, key)}/board`, { body }),
};

// Workspaces and their boards -------------------------------------------------

export const workspaces = {
  list: () => getAll<Workspace>("/workspaces"),
  get: (workspace: string) => get<Workspace>(`/workspaces/${enc(workspace)}`),
  create: (name: string) => create<Workspace>("/workspaces", { name }),
  /** The board's cards without their comments (the board does not show them); `state` defaults to open. */
  cards: (workspace: string, state: "open" | "all") =>
    getAll<Card>(`/workspaces/${enc(workspace)}/board/cards`, { state, comments: "none" }),
};

// Pull requests --------------------------------------------------------------

const pullPath = (repo: string, number: number) => `/repos/${enc(repo)}/pulls/${enc(number)}`;

/** The most file entries the review screen loads for one pull request. */
export const MAX_FILES = 300;

export const pulls = {
  get: (repo: string, number: number) => get<PullRequest>(pullPath(repo, number)),
  update: (
    repo: string,
    number: number,
    body: { title?: string; body?: string; draft?: boolean; tickets?: TicketLink[]; revision?: number },
  ) => request<PullRequest>("PATCH", pullPath(repo, number), { body }),
  activity: (repo: string, number: number) => get<Activity>(`${pullPath(repo, number)}/activity`),
  threads: (repo: string, number: number) => getAll<Thread>(`${pullPath(repo, number)}/threads`),
  commits: (repo: string, number: number) => getAll<PullCommit>(`${pullPath(repo, number)}/commits`),
  files: (repo: string, number: number) =>
    getAll<FileChange>(`${pullPath(repo, number)}/files`, {}, { maxItems: MAX_FILES }),
  checks: (repo: string, number: number) => get<Checks>(`${pullPath(repo, number)}/checks`),
  comment: (repo: string, number: number, body: string) =>
    create<Comment>(`${pullPath(repo, number)}/comments`, { body }),
  review: (repo: string, number: number, verdict: Verdict, body: string, commit: string) =>
    create<Review>(`${pullPath(repo, number)}/reviews`, { verdict, body, commit }),
  reply: (repo: string, number: number, threadId: string, body: string) =>
    create<ThreadComment>(`${pullPath(repo, number)}/threads/${enc(threadId)}/replies`, { body }),
  resolve: (repo: string, number: number, threadId: string, resolved: boolean) =>
    request<unknown>("PUT", `${pullPath(repo, number)}/threads/${enc(threadId)}/resolved`, { body: { resolved } }),
  requestReviewer: (repo: string, number: number, name: string) =>
    request<unknown>("PUT", `${pullPath(repo, number)}/reviewers/${enc(name)}`),
  withdrawReviewer: (repo: string, number: number, name: string) =>
    request<unknown>("DELETE", `${pullPath(repo, number)}/reviewers/${enc(name)}`),
  react: (repo: string, number: number, targetId: string, reaction: Reaction, on: boolean) =>
    request<unknown>(on ? "POST" : "DELETE", `${pullPath(repo, number)}/reactions`, { body: { target_id: targetId, reaction } }),
  merge: (repo: string, number: number, expectedHead: string, expectedBase: string) =>
    request<PullRequest>("POST", `${pullPath(repo, number)}/merge`, {
      body: { expected_head: expectedHead, expected_base: expectedBase },
    }),
  resumeLanding: (repo: string, number: number, expectedHead: string, expectedBase: string) =>
    request<PullRequest>("POST", `${pullPath(repo, number)}/landing/resume`, {
      body: { expected_head: expectedHead, expected_base: expectedBase },
    }),
  /** What publishing to the upstream host would post. */
  publishPreview: (repo: string, number: number) => get<PublishPreview>(`${pullPath(repo, number)}/publish`),
  publish: (repo: string, number: number, expectedHead: string, expectedBase: string) =>
    request<PullRequest>("POST", `${pullPath(repo, number)}/publish`, {
      body: { expected_head: expectedHead, expected_base: expectedBase },
    }),
  close: (repo: string, number: number) => request<PullRequest>("POST", `${pullPath(repo, number)}/close`),
  reopen: (repo: string, number: number) => request<PullRequest>("POST", `${pullPath(repo, number)}/reopen`),
};

// CI -------------------------------------------------------------------------

const jobPath = (repo: string, id: string) => `/repos/${enc(repo)}/jobs/${enc(id)}`;

export const jobs = {
  get: (repo: string, id: string) => get<Job>(jobPath(repo, id)),
  log: (repo: string, id: string, tailBytes: number) => get<JobLog>(`${jobPath(repo, id)}/log`, { tail_bytes: tailBytes }),
  retry: (repo: string, id: string) => request<Job>("POST", `${jobPath(repo, id)}/retry`),
  cancel: (repo: string, id: string) => request<unknown>("POST", `${jobPath(repo, id)}/cancel`),
  artifactUrl: (repo: string, sha: string, name: string) =>
    `/api/v1/repos/${enc(repo)}/commits/${enc(sha)}/artifacts/${enc(name)}`,
};

// Administration -------------------------------------------------------------

export const admin = {
  diagnostics: () => get<Diagnostics>("/admin/diagnostics"),
  ci: () => get<CiSettings>("/admin/ci"),
  setRunner: (mode: CiSettings["runner"]["mode"]) => request<CiSettings>("PUT", "/admin/ci/runner", { body: { mode } }),
  setLimits: (limits: CiLimits) => request<CiSettings>("PUT", "/admin/ci/limits", { body: limits }),
  /** Starts an image operation (one at a time); the answer's `operation` is running. */
  pullImage: (reference: string) => request<CiSettings>("POST", "/admin/ci/images/pull", { body: { reference } }),
  loadImage: (path: string) => request<CiSettings>("POST", "/admin/ci/images/load", { body: { path } }),
  webhook: () => get<Webhook>("/admin/webhook"),
  setWebhook: (url: string) => request<Webhook>("PUT", "/admin/webhook", { body: { url } }),
  deleteWebhook: () => request<Webhook>("DELETE", "/admin/webhook"),
};
