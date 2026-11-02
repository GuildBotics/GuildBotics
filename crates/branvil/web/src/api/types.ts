// Shapes of the Branvil HTTP API v1 (docs/api.md). Only the fields the GUI
// reads are typed; the service may send more.

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export type MemberKind = "owner" | "agent" | "runner";

/** A member: the owner, an agent, or a runner; its kind decides what it may do. */
export interface Member {
  name: string;
  display_name: string;
  kind: MemberKind;
  /** Accounts on upstream hosts, one per provider (tokens are never returned). */
  accounts: Account[];
  /** The members asked to review every pull request this member opens. */
  reviewers: string[];
  /** Where the member's picture is (the address changes with it); null for none. */
  avatar_url?: string | null;
}

/** A member's account on an upstream host: a token of its own, or an app's identity. */
export interface Account {
  provider: string;
  login: string;
  kind: "token" | "app";
  set_at: string;
}

/** What an account is set with; sent once and never returned. */
export type AccountCredential = { token: string } | { app: { app_id: string; installation_id: string; private_key: string } };

/** The upstream hosts this build knows, by provider name. */
export const PROVIDERS = [{ name: "github", label: "GitHub" }] as const;

export interface Session {
  me: Member;
  csrf: string;
}

export interface TokenInfo {
  id: string;
  label: string;
  created_at: string;
  last_used_at: string | null;
}

export interface IssuedToken {
  id: string;
  label: string;
  token: string;
  created_at: string;
}

export interface Repository {
  path: string;
  default_branch: string;
  clone_url: string;
  url: string;
  protected_branches: string[];
  required_checks: string[];
  /** The approvals a merge or a publish needs; 0 needs none. */
  required_approvals: number;
  /** Its pull requests land by themselves (merged, or published to the upstream) once they can. */
  auto_land: boolean;
  /** Whether its CI jobs may reach public network destinations. */
  ci_network: boolean;
  /** The labels its tickets may carry. */
  labels: string[];
  /** The upstream host it follows and publishes to. */
  upstream: Upstream | null;
  revision: number;
}

export interface Upstream {
  provider: string;
  /** The provider's name for people ("GitHub"). */
  host: string;
  repository: string;
  url: string;
  /** Whose account reads it; `member` is "" when no member has an account there. */
  reader: { member: string; chosen: boolean };
  following: {
    tip: string | null;
    checked_at: string | null;
    followed_at: string | null;
    error: string | null;
    stopped: string | null;
  };
}

export interface Commit {
  sha: string;
  message: string;
  author: string;
  authored_at: string;
  parents: string[];
}

/** A commit of a pull request (`base..head`); `committed_at` is UTC like the service's own times. */
export interface PullCommit {
  sha: string;
  author: string;
  committed_at: string;
  subject: string;
}

export const LANES = ["new", "ready", "in_progress", "done"] as const;
export type Lane = (typeof LANES)[number];

export type Reaction = "ack" | "agree" | "celebrate" | "support";
export const REACTIONS: Reaction[] = ["ack", "agree", "celebrate", "support"];

export interface ReactionEntry {
  author: string;
  reaction: Reaction;
}

export interface Comment {
  id: string;
  author: string;
  body: string;
  meta: Record<string, unknown> | null;
  created_at: string;
  reactions: ReactionEntry[];
  url: string;
}

/** An image attached to a repository's tickets; `url` is the same-origin path a ticket's Markdown shows it by. */
export interface Attachment {
  id: string;
  name: string;
  media_type: string;
  size: number;
  url: string;
  created_at: string;
}

export interface ThreadComment extends Comment {
  reply_to_id?: string | null;
}

export interface Assignee {
  name: string;
  assigned_at: string;
}

export interface Ticket {
  url: string;
  repository: string;
  /** The ticket's number in its repository, as a string ("1"). */
  key: string;
  title: string;
  body: string;
  state: "open" | "closed";
  state_reason: "completed" | "not_planned" | null;
  author: string;
  assignee: Assignee | null;
  labels: string[];
  /** A person works on it interactively: members leave it to them. */
  interactive: boolean;
  /** The nearest ancestor whose own `interactive` is on: it makes this one interactive too. */
  interactive_ancestor: TicketRef | null;
  /** The issue it stands for on its repository's upstream host. */
  upstream_issue: string | null;
  /** The lane on its workspace's board (every ticket is on it). */
  lane: Lane;
  rank: string;
  /** The ticket this one is a part of: it completes only once its children are closed. */
  parent: TicketRef | null;
  children: TicketRef[];
  /** The tickets that must be closed before this one is started (moved into in_progress). */
  blocked_by: TicketRef[];
  /** The tickets this one must be closed before. */
  blocks: TicketRef[];
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  revision: number;
}

/** A ticket another ticket is related to (same workspace). */
export interface TicketRef {
  url: string;
  repository: string;
  key: string;
  title: string;
  state: "open" | "closed";
  state_reason: "completed" | "not_planned" | null;
}

/**
 * How a ticket is named where tickets of several repositories meet
 * (`acme/demo#1`): numbers repeat across repositories. It is also the card
 * name `PUT .../board` takes in `before`/`after`.
 */
export const ticketRef = (t: Pick<Ticket, "repository" | "key">) => `${t.repository}#${t.key}`;

export interface PullSummary {
  url: string;
  label: string;
  title: string;
  state: "open" | "closed";
  merged: boolean;
  updated_at: string;
  closes?: boolean;
}

export interface Card extends Ticket {
  comments: Comment[];
  pull_requests: PullSummary[];
}

/** A workspace: its repositories are `<name>/<repository>`, and it has one board. */
export interface Workspace {
  name: string;
  /** The lanes of its board. */
  lanes: Lane[];
}

export interface TicketLink {
  url: string;
  closes: boolean;
}

/** A statement on a pull request that waits on its author's answer: the entry the author answers. */
export interface Unanswered {
  kind: "comment" | "review" | "thread";
  id: string;
  author: string;
  url: string;
}

export interface PullRequest {
  url: string;
  repository: string;
  number: number;
  label: string;
  title: string;
  body: string;
  state: "open" | "closed";
  merged: boolean;
  draft: boolean;
  author: string;
  head: string;
  head_repository: string;
  head_sha: string;
  base: string;
  base_sha: string;
  reviewers: string[];
  /** Every member's part in the review, as the server decides it. */
  review_states: ReviewState[];
  /** The members whose state approves: not requested again, `approve` on the current head. */
  approvals: number;
  /** The statements that wait on the author's answer; none while the pull request awaits none (its author is the owner, or it links an interactive ticket). */
  unanswered: Unanswered[];
  tickets: TicketLink[];
  labels: string[];
  changed_files: number;
  ready_at: string | null;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
  merged_at: string | null;
  merge_sha: string | null;
  /** Set once published to the repository's upstream host. */
  upstream: Published | null;
  landing: Landing | null;
  revision: number;
}

export type LandingState = "waiting" | "gating" | "passed" | "left";

export type LandingNext = "gate" | "publishing" | "owner_publish" | "host_checks" | "owner_merge_host" | "following" | "owner_fix_following" | "host_closed" | "owner_fix_auto" | "auto_merge" | "owner_merge";

export interface LandingFirst {
  number: number;
  next: LandingNext;
  host_url: string | null;
  next_detail: string | null;
  state: LandingState;
  state_at: string;
  error: string | null;
}

/** One pull request in its base branch's landing queue. */
export interface Landing {
  number: number;
  title: string;
  pull_state: "open" | "closed";
  draft: boolean;
  base: string;
  queued_at: string;
  resume_publish: boolean;
  error: string | null;
  state: LandingState;
  state_at: string;
  gate: string | null;
  base_sha: string;
  head_sha: string;
  reason: { code: string; message: string; jobs: string[]; files: string[] } | null;
  position: number | null;
  first: LandingFirst | null;
}

/** A pull request published to the upstream host. */
export interface Published {
  /** Where it was published; it stays there if the repository's upstream changes. */
  provider: string;
  repository: string;
  /** The member whose account posts it there (the author). */
  account: string;
  number: number | null;
  url: string | null;
  /** The host's state; `none` until a pull request is open there. */
  state: "none" | "open" | "closed" | "merged";
  pushed: string | null;
  published_at: string;
  error: string | null;
  /** Why sending the branch stopped; publishing again replaces what the host has. */
  stopped: string | null;
  /** Published by Branvil itself, the repository landing automatically. */
  auto: boolean;
}

/** The webhook told of automatic publishes; its URL is never shown, only its host. */
export interface Webhook {
  set: boolean;
  host: string | null;
  /** Why the last send failed. */
  problem: string | null;
}

/** What publishing would post. */
export interface PublishPreview {
  host: string;
  repository: string;
  head: string;
  base: string;
  title: string;
  body: string;
  account: string;
  login: string | null;
}

export type Verdict = "approve" | "request_changes" | "comment";

/** A member's part in a pull request's review: an open request, and their verdict (a comment keeps an earlier one). */
export interface ReviewState {
  member: string;
  requested: boolean;
  verdict: Verdict | null;
  /** That review is on a commit other than the current head. */
  outdated: boolean;
}

export interface Review {
  id: string;
  author: string;
  verdict: Verdict;
  body: string;
  commit: string;
  submitted_at: string;
  reply_only?: boolean;
  reactions: ReactionEntry[];
}

export interface Thread {
  id: string;
  resolved: boolean;
  outdated: boolean;
  path: string;
  line: number;
  side: "LEFT" | "RIGHT";
  start_line?: number | null;
  start_side?: "LEFT" | "RIGHT" | null;
  commit?: string;
  comments: ThreadComment[];
}

export interface Activity {
  comments: Comment[];
  reviews: Review[];
  threads: Thread[];
}

export interface FileChange {
  path: string;
  previous_path: string | null;
  status: "added" | "removed" | "modified" | "renamed";
  additions: number;
  deletions: number;
  patch: string | null;
  patch_complete: boolean;
}

export type CheckStatus = "queued" | "in_progress" | "completed";
export type CheckConclusion =
  | "success"
  | "failure"
  | "cancelled"
  | "timed_out"
  | "skipped"
  | "startup_failure"
  | null;

export interface ReusedFrom {
  attempt_id: string;
  url: string;
  sha: string;
}

export interface PathSkip {
  paths: string[];
  bases: { pull: number; base: string; base_sha: string; merge_base: string | null }[];
}

export interface Check {
  path_skip?: PathSkip | null;
  name: string;
  status: CheckStatus;
  conclusion: CheckConclusion;
  attempt_id?: string;
  attempt?: number;
  details_url?: string;
  details?: string;
  artifact_names?: string[];
  reused_from?: ReusedFrom;
  cache_refresh?: string;
  started_at?: string;
  finished_at?: string;
  /** For an unfinished check: how long the job took where it last succeeded before. */
  expected_secs?: number;
}

/** The checks of one commit (docs/api.md, "Checks"). */
export interface CheckSet {
  checks: Check[];
  checks_expected: boolean;
}

export interface Checks extends CheckSet {
  head_sha: string;
  base_sha: string;
  /** null when a branch is gone. */
  behind_by: number | null;
  base_checks: number;
}

export interface CommitChecks extends CheckSet {
  sha: string;
  run_registered: boolean;
}

export interface Branch {
  name: string;
  sha: string;
}

export interface Job {
  path_skip?: PathSkip | null;
  purpose: "check" | "cache_refresh";
  cache_refresh: string | null;
  id: string;
  job: string;
  attempt: number;
  sha: string;
  status: CheckStatus;
  conclusion: CheckConclusion;
  started_at: string | null;
  finished_at: string | null;
  details: string | null;
  artifact_names: string[];
  reused_from: ReusedFrom | null;
}

export interface JobLog {
  log: string;
  log_bytes: number;
  truncated: boolean;
}

/** What `/admin/diagnostics` says that the warnings use; the rest is for people reading the API. */
export interface Diagnostics {
  runner?: { state: "ready" | "no_runner" | "executor_unavailable" | "stopping" | string; detail: string; mode: string };
  upstream?: { state: string; problems?: { repository: string; problem: string }[] };
}

/** The CI runner, its executor, job limits, and image cache, as `/admin/ci` shows them to the owner. */
export interface CiSettings {
  runner: {
    /** The `[ci] runner` setting as it applies now. */
    mode: "auto" | "off";
    state: "ready" | "no_runner" | "executor_unavailable" | "stopping";
    detail: string;
    runners: CiRunner[];
  };
  executor: {
    /** The msb in use, or null when none is usable (`problem` says why). */
    msb: string | null;
    problem: string | null;
    /** The image cache. */
    home: string;
  };
  limits: CiLimits;
  images: {
    /** Why the cache could not be read, or null. */
    error: string | null;
    cached: CachedImage[];
    /** The images pipelines name that the cache cannot run; empty when none is missing. */
    missing: MissingImage[];
  };
  operation: ImageOperation | null;
  repositories: { path: string; ci_network: boolean }[];
}

export interface CiRunner {
  name: string;
  last_seen_at: string;
  executor: string;
  executor_state: string;
  executor_detail: string | null;
}

export interface CiLimits {
  max_cpus: number;
  max_memory_mib: number;
  max_timeout_secs: number;
  cache_max_mib: number;
  max_jobs: number;
}

export interface CachedImage {
  /** The tag: what to write as `image` in `.branvil/ci.toml`. */
  reference: string;
  /** The configuration digest: which version of the tag the cache holds. */
  digest: string;
  size_bytes: number;
  created_at: string;
  /** The repositories whose default-branch pipeline names this image; empty when none does. */
  used_by: ImageUse[];
}

/** A repository whose default-branch pipeline names an image, and the jobs that do. */
export interface ImageUse {
  repository: string;
  jobs: string[];
}

/** An image pipelines name that the cache does not hold. */
export interface MissingImage {
  image: string;
  repositories: ImageUse[];
}

export interface ImageOperation {
  kind: "pull" | "load";
  /** The reference pulled or the path loaded. */
  target: string;
  state: "running" | "succeeded" | "failed";
  detail: string | null;
  started_at: string;
  finished_at: string | null;
  /** The images the operation brought, also those already in the cache. */
  loaded: { reference: string }[];
}

export type ChangeKind ="ticket" | "pull" | "board" | "check" | "upstream" | "repo";

export interface ChangeEvent {
  kind: ChangeKind;
  path?: string;
  id?: string;
}
