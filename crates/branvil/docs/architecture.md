# Architecture

Branvil is one Rust process (`branvil serve`) plus Git child processes, and a
separate CI runner process (`branvil runner`) that executes jobs in microVMs. It
listens on loopback only; other machines reach it through OpenSSH port forwarding
managed by an OS service (see `README.md`). The HTTP API is the only way into the
data: the GUI, the GuildBotics `branvil` provider, and the `branvil` CLI's
admin commands all use it.

```
browser GUI ─┐                         ┌─ redb (branvil.redb)
provider ────┼─ HTTP (axum) ─ domain ──┤
branvil CLI ─┘        │                └─ git (bare repos, argv only)
git client ── Smart HTTP ─ git http-backend ─ receive-pack ─ Branvil hooks ─┐
                                                                            └─ loopback callback ─ refs
branvil runner ── runner API (HTTP) ─ ci ─ redb queue · ci/ files      (no database, no repositories)
     └─ msb run ─ microVM per job (only its work and log directories mounted, no network)
```

## Modules (`src/`)

| Module | Responsibility |
| --- | --- |
| `main.rs`, `cli.rs` | clap commands: `init`, `serve`, `admin check|reindex|upgrade` (offline, on the data directory), `admin notify` (reads only the webhook credential, online or offline), and `member`/`workspace`/`repo`/`backup`/`open` (clients of the running API, with the owner's token file by default) |
| `config.rs` | `branvil.toml`, data directory layout, loopback-only listen, fixed base URL |
| `store.rs` | redb tables, order-preserving keys, JSON records, bounded blocking executor (`Store::read` / `Store::write`), the schema version and the upgrade frame (exclusive open, reads, then a copy and one write transaction) |
| `upgrade.rs` | what the upgrade from the previous schema version changes in the records (replaced at each version) |
| `model.rs` | stored records and the one function per record that derives its index entries; consistency check and rebuild |
| `auth.rs` | tokens (`bvt_<id>_<secret>`, SHA-256 stored, constant-time compare), the owner's token file (`secrets/owner.token`, replaced at start when missing or invalid), sessions with CSRF secret, the checks of what a member's kind may do |
| `domain/` | the operations: `members` (also entry links and sessions), `repos` (workspaces and their repositories), `tickets` (a repository's tickets, comments, the workspace's board and its cards), `attachments` (images of a repository's tickets, files in `<data>/attachments/`, and the hourly deletion of those no ticket refers to), `pulls` (PRs, reviews, threads, diffs, checks, merge) |
| `git.rs` | Git commands with a sanitized environment (no `GIT_*` of the service, no system/global config), time/output/concurrency limits |
| `smart_http.rs` | `git http-backend` as CGI with Basic auth and streaming bodies |
| `hooks.rs` | Branvil-owned hook scripts and the `branvil hook` entry point they exec |
| `refs.rs` | the ref-change boundary: snapshot recording, journal, startup recovery, push policy |
| `ci/` | CI in the service (see below and `docs/ci.md`): `pipeline` (schema), `queue` (runs, attempts, every state change), `files` (logs, artifacts, retention), `api` (job and runner endpoints), `supervise` (`[ci] runner = "auto"`) |
| `runner/` | `branvil runner`: the claim/heartbeat/finish loop, `executor` (the `Executor` trait and the microsandbox implementation over the `msb` CLI), `workspace` (source checkout, artifacts without links, dependency cache), `zip` |
| `upstream/` | the upstream code host: the provider boundary (`host.rs`, `github.rs`), members' accounts, following the default branch, publishing pull requests, carrying changes both ways, the host's check runs (see below) |
| `landing.rs` | persistent queues per repository/base, admission and departure, gate commits, gate checks, and failure comments |
| `webhook.rs` | bounded webhook delivery shared by the service and offline CLI; never exposes the URL in errors |
| `autoland.rs` | the task that advances every repository's landing queues, automatically merges or publishes with `auto_land`, and tells the webhook when automatic landing gates pass; independently sweeps CI failure notifications (`ci/notify.rs`) |
| `backup.rs` | `POST /admin/backup` (online, consistent) and the offline `branvil restore` (`docs/backup-restore.md`) |
| `events.rs` | bounded broadcast for SSE and the landing task |
| `http.rs` | router, Host/Origin guard, security headers, idempotency middleware, extractors, SSE, GUI files |
| `references.rs` | Markdown reference syntax and prose ranges, shared by display and upstream rewriting |
| `rank.rs` | card ranks (fractional indexing) |

## Requests

`http.rs` applies, in order: the Host/Origin guard (every route except the
hook callbacks), then for `/api/v1` the idempotency middleware (POST with
`Idempotency-Key`), then the handler. Handlers authenticate through the
`Caller` extractor (Bearer token, or session cookie plus `X-Branvil-CSRF` on
writes), parse the request, and call one `domain` function. Domain functions
check what the caller's kind may do (`Caller::require_owner`,
`Caller::require_agent_or_owner`: `403` otherwise), read Git when they need tips
or diffs, and then run one short write transaction.

## Git hosting

- Repositories are bare, at `<data>/repos/<path>.git`, created by
  `POST /api/v1/repos`. Each carries `core.hooksPath=<data>/hooks`,
  `transfer.hideRefs=refs/branvil`, `receive.fsckObjects=true`,
  `core.logAllRefUpdates=always`, and `http.lowSpeedLimit=1` with
  `http.lowSpeedTime` set to `limits.git_command_timeout_secs` (a stalled
  upstream host ends a fetch or push even when nothing times it out any more,
  below); these are re-applied at every start.
- Smart HTTP spawns `git http-backend` with an argv array and an environment
  built from scratch for the request (`PATH_INFO` uses the stored spelling of
  the path). `core.hooksPath` is also passed through `GIT_CONFIG_COUNT`, so
  even a modified repository configuration cannot select another hooks
  directory. Repository-provided hooks never run (`tests/git_http.rs`).
- Limits: `limits.git_http_concurrency` Smart HTTP transfers and
  `limits.git_concurrency` service-side Git commands, in separate pools: a
  push's hook waits for the service to record the push, which runs service
  commands while the push holds its transfer slot (`tests/git_http.rs`),
  `limits.git_http_timeout_secs` per Smart HTTP request
  (the child is killed), `limits.git_push_max_mib` request body,
  `limits.git_command_timeout_secs` and 64 MiB of output per service command.
- Service-side Git children (Smart HTTP's included) run in a process group of
  their own, so a terminal's Ctrl-C or a supervisor that kills the service's
  group never reaches Git in the middle of a ref update: Git killed there
  leaves its lock files (`packed-refs.lock`, `<ref>.lock`), and the repository
  refuses every later update of those refs. A service command past its time
  limit gets SIGTERM, with the hooks it started (its process group), on
  which Git removes its lock files, and Git itself gets SIGKILL only 2 s
  later; a dropped read is killed. A Smart HTTP request past its own time
  limit has its `http-backend` killed at once.
- **One generation of Git at a time.** A running service holds
  `<data>/git/service.lock`, taken right after it opens the database and
  before it touches any repository (`git::hold`), and every service-side Git
  child, and what it starts in turn (receive-pack, hooks, a detached
  `git gc --auto`, a backup's `git bundle`), inherits it (the runner and
  every other child do not). The lock is free only once all of them have
  exited, so a service that died with Git still running (a crash, `kill -9`,
  a supervisor's deadline, the OOM killer) leaves it held: the next `serve`
  logs that it waits and tries again every 100 ms (SIGINT or SIGTERM ends it
  then), so a gc or a backup's bundle the killed service left running delays
  the next start too. Only then does startup remove the lock files a killed
  Git left in each repository (`git::remove_stale_locks`: its own, its refs'
  and reflogs', and gc's in `objects/info` and `objects/pack`): no Git that
  could own one runs any more. On Windows the children do not inherit the
  lock (it ends with the service): neither this boundary nor the removal
  applies there, and a push whose receive-pack is still running when the
  service stops may update its refs after the next start's recovery.

## Ref changes and recovery

Git ref updates and redb commits are separate, so Branvil never assumes they
are atomic together.

1. **One boundary per repository, and operations that run to their end.**
   `App::locked(repo id, op)` takes the repository's async mutex for every ref
   change Branvil makes (merge, gates, the default branch's `HEAD`) and every
   recording of ref changes (post-receive, startup rescan), and runs `op` in a
   task of its own. Waiting for the lock is
   cancellable; once it is held, `op` runs to its end even when its caller
   stops waiting (a client that closed its request, a task of a stopping
   service), so Git and the database are never left halfway by a caller.
   `App::finish(op)` does the same for writes that also talk to the upstream
   host (publishing a pull request, following the upstream's default branch,
   a poll's work on one published pull request, connecting an upstream): they
   take the lock only around their own changes (`App::lock_repo`), never while
   waiting for the host. An operation never starts another one (checked in
   debug builds). A stopping service starts no new operation and waits for the
   running ones (`App::close`): they usually take milliseconds, and each Git
   command and host request is bounded by its own time limit (see
   `docs/ci.md`, "Stopping the service").
2. **Pushes** are checked before and recorded after:
   - `pre-receive` (Branvil's script → `branvil hook pre-receive` → `POST
     /-/hooks/pre-receive` on the bound loopback address with a per-process
     secret from the Git child's environment) applies `refs::check_push`:
     no runner, branches and tags only, protected branches never pushed,
     deleted, or force-pushed. It fails closed when the service cannot be
     reached.
   - `post-receive` asks the service to run `refs::record_refs`: under the
     repository lock it reads every ref, diffs it with `ref_snapshots`, and in
     one transaction stores the new snapshot and marks open pull requests whose
     head moved as updated. Then CI is told (`Ci::on_ref_changes`), the
     upstream poller is woken when a published pull request's head moved
     (`Upstream::on_ref_changes`, which writes nothing), and an SSE event is
     published.
3. **Merges** (`domain::pulls::merge_pull`, the owner's or the landing
   task's), under the repository lock:
   re-read the PR and tips; check expected head/base, the passed current gate
   at the front of the queue, required checks, and required approvals (`check_landable`, which publishing shares;
   it counts the review states of `review_states`, the one place they are
   decided, which the pull request's view carries too); point the hidden ref of the merged head at it (`keep_merged`,
   below); write a `ref_journal` entry `{ref, old, new, op}`; run
   `git update-ref refs/heads/<base> <gate> <old>` (Git refuses if the base
   moved; when Git fails, the ref tells what happened: at `<gate>` the merge
   goes on, anywhere else nothing was written and the entry is deleted); then
   one transaction applies the merge (`apply_journal`: PR merged,
   `closes` tickets closed and moved to `done`) and deletes the journal entry;
   then the head branch is retired (`retire_merged_head`, below);
   finally `record_refs_locked` records the snapshot and notifies CI. The
   recording takes a branch's move before a deletion, so a commit merged into
   the default branch keeps its CI run; merged into another branch, which CI
   does not run, its unfinished jobs are superseded with the head.
   - **The head of a merged pull request** (here or on the upstream host) is
     the reviewed head (`merged_head`, or `upstream.pushed` when merged on the
     host). `merge_sha` is the commit placed on the base: the gate for a local
     merge, or the host's merge commit. The reviewed head may differ from it.
     Before the merge is recorded, the hidden `refs/branvil/merged/<number>` is
     pointed at the reviewed head, so every merged pull request has it: a squash or a rebase on the host leaves it on no
     branch, and the pull request's view, backups (`git bundle --all`), and
     Git's pruning all go by reachability. After the merge is recorded, the
     branch is deleted only while it is still at the reviewed head
     (`git update-ref` with that old value). It stays when it is protected or
     when an open pull request has it as its head or base. Only Branvil's
     branch is deleted, never the host's. If the process stops before this
     step, the branch stays.
4. **Startup** (`refs::recover`, before the listener accepts requests): every
   pending journal entry is compared with the actual ref. At `new`, the
   operation's database side is applied (idempotently); at `old`, it is
   dropped; anywhere else it is dropped with an error log. Then every
   repository is rescanned, and missing gate refs are restored from their queue
   records before any request, including a backup, can run. Thus gate objects
   remain reachable in backups after an interrupted gate creation. Pushes accepted while the service was down or
   between Git and the database are recorded.

`BRANVIL_FAILPOINT=merge_before_update_ref|merge_after_update_ref|post_receive_before_record`
aborts the process at that point. `tests/recovery.rs` uses it to stop between
Git and the database and checks that the next start converges. This simulates
a killed process, not power loss.

Reviews and inline comments read the head from Git just before their write
transaction and refuse (`409 head_moved`, nothing written) when it is not the
`commit` sent. A push landing after that read is ordered after the review, so
the review stays attached to the commit the reviewer saw. A merge names the
exact head it merges (`expected_head`), so the owner merges what they saw.

## CI

The rest of the service reaches CI only through `Ci` (`src/ci/mod.rs`):

```rust
impl Ci {
    pub async fn head_checks(&self, repo: &Repo, sha: &str) -> AppResult<Checks>;
    pub async fn on_ref_changes(&self, repo: &Repo, changes: &[RefChange]); // under the repo lock
    pub async fn on_pull_opened(&self, app: &App, repo: &Repo, head: &str);  // takes the repo lock
    pub async fn diagnostics(&self) -> serde_json::Value;
}
```

- `head_checks` feeds `GET …/pulls/{n}/checks` (head and base) and the merge
  precondition (every required check `completed`/`success` on the exact head).
  It reads the commit's run and the latest attempt of each job, and adds
  explanatory entries for what is missing; it never invents a success.
- `on_ref_changes` runs after every recorded batch of ref changes (push,
  merge, startup rescan) under the repository lock: the old commit of each
  moved branch may be superseded, the new tip of the default branch or of an
  open pull request head or a current `refs/branvil/gate/<number>` gets a run.
  The pipeline is read from Git before the write transaction (`git cat-file`), from the commit itself.
- A background task (started by `serve` after recovery) sweeps every 5 s:
  expired leases are interrupted (re-queued once), queued work fails with
  `startup_failure` when no runner has been seen for `ci.runner_absent_secs`,
  waiting runs are admitted; at start and every minute the reconciler
  registers the default-branch tips, open pull request heads, and current gate
  refs again (a missing run is created, jobs without a current attempt are
  queued); every hour retention deletes old logs and artifacts. At start it removes orphaned files.
- The runner API (`src/ci/api.rs`) is the runner's only access: the source
  bundle is produced by the service (`git pack-objects` behind a bundle
  header, a Smart HTTP transfer permit, the transfer timeout), logs and artifacts are written to files
  before they are recorded, and every call checks the lease in the same
  transaction that changes the attempt.

The runner (`src/runner/`) is a separate process with its own failure domain:
it holds no database handle, no repository, and no secret except its own
token. Its `Executor` trait has one implementation, microsandbox; tests use a
fake executor that starts no process. Details, limits, and the trust model:
`docs/ci.md`.

## Upstream: the code host above, Branvil in front

Branvil is where agents try things: many pushes, review rounds, and CI runs,
locally and without cost. A repository may have an **upstream** on a code host
(GitHub today), which stays the place of record: its default branch is
protected there, pull requests are approved and merged there by people, its CI
(Windows included) and releases run there, and people outside see the finished
pull requests there. Branvil never pushes the default branch; it follows it.

What the owner sees (GitHub as the example):

1. Agents work in Branvil as before. Nothing reaches GitHub.
2. On a pull request of a repository with an upstream, the merge button is
   replaced by **Publish to GitHub**. It shows exactly what will be posted (the
   title and the current body, plus `Closes`/`Refs` lines for linked tickets
   that name a GitHub issue) and has the same preconditions as a merge (open,
   not a draft, a passed current gate at the front of its base's queue,
   the required checks succeeded on the head, the required approvals of the
   head) plus two more: the base is the default branch, and the pull request's
   author has a GitHub account in Branvil.
3. Branvil pushes the head branch to GitHub and opens a pull request there with
   that title and body, **as the author's account** (an agent's bot account, as
   when agents worked on GitHub directly), so a person can approve it where
   approvals are required. Conversation, reviews, and threads stay in Branvil.
   The Branvil pull request shows the GitHub pull request's link and state.
4. GitHub's check runs on the published head appear among the Branvil pull
   request's checks as `github/<name>`, with their logs through the job-log
   endpoint (`github-<check run id>`), so agents fix failures in Branvil. Every
   later push to the branch, edit of the title or body, and close or reopen in
   Branvil is carried to the GitHub pull request.
5. A person approves and merges on GitHub. Branvil fetches GitHub's default
   branch, fast-forwards its own, marks its pull request merged (with GitHub's
   merge commit), closes the tickets it closes, and deletes its head branch in
   Branvil (as a merge in Branvil does; GitHub's stays). A pull request closed on
   GitHub without a merge is closed in Branvil too. Closed on both sides, it
   is no longer polled; reopening it in Branvil reopens it on GitHub.

### The provider boundary

`src/upstream/` knows no code host. Everything that does is behind `Host`
(`src/upstream/host.rs`), one variant per provider; `github.rs` is the only one
today. A host:

- names itself (`github`, "GitHub") and validates its repository names
  (`owner/name`);
- gives the Git URL of a repository and the web URL of a repository;
- checks a member's credential (a token, or an app's identity) and answers
  its login, and yields a token for it (GitHub makes an app's installation
  tokens from a JWT signed with the app's key);
- reads a branch's tip and a pull request (state, merged,
  merge commit); creates one, finds the open one with a head, and changes its
  title, body, and state;
- reads the check runs of a commit and one check run's log;
- reads the repository's dependency alerts (GitHub: Dependabot alerts), one
  page from the host's cursor or one by its id, in Branvil's words (`open`,
  `resolved`, `dismissed`), and says what to check when the host refuses;
- recognizes its issue URLs and writes a reference to one from a pull request
  of a repository (`#12`, `owner/other#12`) and the line that closes or refers
  to it.

Adding a provider is adding a variant and its module; nothing else changes.

### Accounts

Each member (the owner and agents) may have one account per provider, which
the owner sets (`PUT /api/v1/members/{name}/accounts/{provider}`) in one of two
kinds:

- **token**: a token of the account (a person's or a machine user's);
- **app**: a GitHub App's identity (app id, installation id, private key), the
  way most agents are set up on GitHub. The account acts as `<slug>[bot]`.

Either is checked with the host when set; the login it reports and the kind
are kept on the member and shown. The credential itself is a `0600` file,
`<data>/secrets/accounts/<member>.<provider>.json` (`{"token": ...}` or
`{"app": {...}}`, the body it was set with), never in redb, a response, a log,
argv, or a URL. Every use of an account (reads, pushes, API calls) gets its
token from one place (`token_of` in `src/upstream/mod.rs`, which asks the
host): a token account's token as is; for an app, an installation token
(valid an hour) that the host module makes from a JWT signed with the key,
keeps in memory only, and makes again when less than five minutes are left.
Git gets the token through `GIT_ASKPASS` (this executable answering from its
environment, `credential.helper` cleared, user name `x-access-token`), the API
as a Bearer header. Backups do not carry it.

- **Writes** (the branch, the pull request) use the account of the Branvil pull
  request's author.
- **Reads** (following the default branch, polling pull requests and check
  runs, logs, dependency alerts) use one member's account, the upstream's *reader*: the host
  counts only authenticated conditional requests as free. The owner may name
  it; otherwise one is picked when the upstream is set: an agent's account
  (the first by name), the owner's only when no agent has one, so the owner's
  own token is not needed. The reader is kept: a new account never replaces
  it. It changes only when the owner names another or asks to pick again, or
  when its account is removed (another one is picked at once, or nobody: then
  polling reports that nobody can read it). A refused token is reported, never
  silently replaced by another account's.

### Records

- `Repo.upstream` (`{provider, repository, reader: {member, chosen}}`) is the
  setting, changed by the owner (`PUT/DELETE /api/v1/repos/{repo}/upstream`).
- `Pull.upstream` (`{provider, repository, number, url, state, author account,
  pushed, sent_title, sent_body, sent_state, checks, checks_registered_by,
  checks_settled, error, stopped}`): where it was published, the host's pull
  request, what Branvil last sent to it, and the check runs of the pushed head
  with whether the host may still add some. It stays tied to the host repository it was
  published to: changing or removing the repository's upstream is always
  allowed, a pull request published before keeps talking to its own place, and
  publishing it to the new upstream makes a new record.
  `sent_*` decides the direction of a difference: Branvil's value differs from
  what was sent → Branvil changed it, send it; the host's state differs from
  what was sent → the host changed it, apply it.
- `Ticket.upstream_issue`: the URL of the issue a ticket stands for on its
  repository's host; without it nothing about the ticket is written there (a
  Branvil number means nothing there).
- What following found last (the host's tip, when, the last error) is kept in
  memory and found again by the next poll.

### Following the host

One task polls; "Check now" polls one repository in the request. Polls of one
repository take turns. A poll never waits for the host while holding the
repository lock or a transaction.

- **Conditional requests.** Every GET carries the `ETag` of its last answer
  (`If-None-Match`, kept in memory); GitHub answers an unchanged resource with
  `304`, which does not count against the rate limit.
- **Interval.** Every 60 s while any published pull request is open (its state,
  the check runs of its pushed head, and the default branch); every 15 min
  otherwise (the default branch only, so merges of other people's pull requests
  arrive). Publishing, `POST /api/v1/repos/{repo}/upstream/refresh` (the GUI's
  "Check now"), a push to a published branch, and an edit of a published pull
  request wake it at once. The next registration deadline of a live pull
  request also wakes it, even with longer regular intervals. An expired
  deadline whose read failed is retried at the regular interval, so it
  cannot cause a busy loop. `[upstream]` in `branvil.toml` sets both intervals.
- **The default branch.** When the host's tip differs from Branvil's: `git
  fetch` it into the hidden `refs/branvil/upstream/<branch>`, then, under the
  repository lock, fast-forward Branvil's branch (`update-ref` with the
  expected old value) and record the ref change. If Branvil's branch has
  commits the host's lacks, following stops with that reason (nothing is
  overwritten; it cannot happen through Branvil, which refuses every push to
  the default branch of a repository with an upstream and refuses its merges).
- **Merged.** A published pull request the host reports merged is marked
  merged only once its merge commit is on Branvil's default branch (followed
  first if needed; a commit only fetched into the hidden ref while following
  is stopped does not count). Its diff stays what Branvil pushed: from where
  the pushed head left the base to the pushed head, whichever way the host
  merged it (a squash or a rebase makes commits of its own). Applying it twice
  is harmless.
- **Live.** A published pull request is polled while it is open in Branvil, or
  open on the host. One the host never opened (its creation kept failing) is
  left alone once closed in Branvil.

### Branvil's references on the host

Branvil writes `#3` for ticket 3 of the pull request's repository,
`<workspace>/<name>#3` for a ticket of another, `!3` for a pull request, and
its own URLs. On the host `#3` names something else (GitHub links `#3`,
`owner/name#3`, `GH-3`, and URLs), so the title and body are rewritten when
they are composed for the host:

- The Markdown is parsed (pulldown-cmark, with tables, strikethrough and task
  lists); only prose is rewritten: code spans, code blocks, links, images,
  and HTML (including text between inline tags) are left alone. Escaped
  sigils stay literal. Inline HTML scope ends at each Markdown block
  boundary, including tight-list items and table cells; an unclosed tag
  cannot suppress references in later blocks. The GUI's resolver uses the
  same `references.rs` parser and `domain::markdown::resolve` target lookup:
  there is no browser reference parser. It inserts links to existing items with kind and current
  state, which the GUI renders through marked and DOMPurify before showing
  the reference icons. `tests/fixtures/markdown-references.json` fixes the
  same examples for Rust parsing, the API resolver, and GUI rendering.
- A ticket that names an issue there becomes the host's reference to that
  issue (`#7`, `owner/other#7`); a pull request published to that repository
  becomes its number there; a Branvil URL of either becomes the host's URL.
- Any other ticket or pull request of Branvil (a ticket without an issue there,
  an unpublished pull request) is written so the host makes no reference of
  it: on GitHub, a code span of Branvil's name for it (`` `acme/demo#3` ``).
- What names nothing in Branvil is left as written: `<a>/<b>#3` where `<a>/<b>`
  is no Branvil repository (it names something on the host), and `#9` when the
  repository has no ticket 9 (a branch name such as `fix/c++#2`, say).

The preview shows the result, so the owner sees it before publishing. When
the result still needs a person's hand, the owner edits the pull request's
title and body in the publish panel (they are Branvil's; the host gets the
change at the next poll). The host's copy is never the original: an edit made
on the host is replaced the next time Branvil's changes.

### Sending to the host

- **Branch.** `git push --porcelain --force-with-lease=refs/heads/<b>:<lease>
  -- <url> <head>:refs/heads/<b>`. Branvil only ever replaces what it put there
  itself: the lease is what Branvil pushed last. Publishing reads the host's
  branch first: when it already is the head (a push whose answer was lost), it
  is not pushed again; when it does not exist, it is created (empty lease);
  when it is something Branvil did not push (a person's branch, an older pull
  request's), nothing is pushed and sending stops with that reason. A commit
  someone adds on the host later, or a refusal by the host, stops sending the
  same way. Publishing again after a stop is the owner's decision to replace
  what the host has (the lease is what the host has then).
- **Pull request.** Created once the branch is there and sending has not
  stopped, or adopted when an open one with that head, opened by the same
  account, already exists (a publish whose answer was lost); someone else's is
  never adopted. Title, body, and state are sent when Branvil's differ from
  what was sent.
- **Failures** (network, credential, host errors) are kept on the record and
  shown; the next poll retries. Nothing in Branvil waits for them.

## Landing queues and automatic landing

Each repository/base branch has a persistent queue. An open, ready pull request
joins its end when its head adds changes, its required head checks succeed,
its head has enough approvals, and no statement awaits its author's answer
(`domain::pulls::unanswered`, the one place that is decided; the pull
request's view carries the same list, and the webhook is told nothing while it
is not empty). GuildBotics' patrol gives the author work on the same
statements, so they must agree on what answers one. With an upstream, the base must also be the
default branch and the author must have an account on the host. Admission does
not change the pull request's revision or activity time. `GET /repos/{repo}/landing`
and each pull request's `landing` show the queue, its state times, and the base,
head, and gate SHAs (`docs/api.md`). These identify what waited and what CI
actually checked, even after a failure.

Only the first pull request in each queue has a gate. If its head contains the
base, the gate is that head and uses its existing CI run. Otherwise
`git merge-tree --write-tree` combines base and head, and `commit-tree` creates
a commit with parents `[base, head]`, with Branvil as author and committer.
The hidden `refs/branvil/gate/<number>` keeps it reachable. This checks the
combined changes without changing the reviewed head or invalidating approvals.

The queue states are `waiting`, `gating`, `passed`, and `left`. Required gate
checks passing changes the first to `passed`; it keeps its place until merged
or removed. With an upstream, this includes the time spent waiting for GitHub's
merge. A local merge fast-forwards the base to the gate commit. Publishing
sends the original head, and later pushes to a published head are sent as before.

- A failed required gate job, merge conflict, stopped publication, or failed
  host check on the head used to build the gate removes the first from the
  queue. Closing, marking draft, changing the head or target base branch, or
  losing the head's checks or approvals, or a statement that awaits the
  author's answer, also removes it; the answer puts it at the end again. A temporary publication
  error keeps its place and shows the error while polling retries. A stopped
  publication returns through the
  owner's `POST /repos/{repo}/pulls/{number}/landing/resume`, with expected
  head/base. Its entry records `resume_publish: true` and keeps the original
  `upstream.stopped` reason while the gate runs. Once passed at the front, the
  owner publishes again to replace the host branch; that action clears the
  intent. Automatic landing never replaces a stopped publication.
- A departed entry with a changed head or base can rejoin at the end. Otherwise
  a failed gate stays out until all its required jobs succeed: retry failed jobs
  and any required jobs cancelled on departure. A failed host check can rejoin
  when it succeeds, and other exclusions can rejoin when their reason clears.
  Conflicts require a head or base change. This avoids rerunning an unchanged
  failed gate on every sweep. Whether such a failure still stands is decided
  once (`landing::failure_stands`); the patrol of `GET /pulls?involves=me`
  counts a pull request held by it as one whose checks failed.
- Advancing base preserves active entries' admission times and order. Waiting
  entries use the current base when their turn arrives. The first entry gets
  a new gate in the same place when base advances during its gate. For a passed
  entry published to the repository's current upstream, a combined tree equal
  to the new base means the changes already landed: it keeps the gate and
  waits for the poller to mark the pull request merged. If the host then
  confirms the pull request is still open, the changes arrived through another
  integration: it leaves with `already_in_base`.
- Gate failures and conflicts add a comment by the reserved author `branvil`.
  It names the base, head, and gate SHAs, failed jobs and log links or conflicting
  files, and how a successful gate retry returns the pull request to the queue.
  A `host_failed` departure adds one with the head and the failed host checks'
  links: nothing else tells the author of a failure on the host. These comments
  carry no `meta`, so they await the author's answer like any other statement:
  the pull request rejoins once the failure is cleared and the author answered.
  The GUI gives this author Branvil's icon; it is not a member account.

Gate refs follow the same ref-change boundary as branches. CI registers a run
only when the queue entry owns that gate SHA, under `gate/<number>`, with a
branch cache scope that cannot save dependency caches. Moving or deleting a
gate ref supersedes its unfinished work when no other CI ref needs the commit.
Leaving or merging deletes the gate ref. The reconciler registers current gate
refs at startup and every minute, including after a crash between ref creation
and CI registration. A failed gate stays reachable through
`refs/branvil/failed-gate/<number>` while the open pull request waits for a retry
with the same head and base, including after it rejoins behind another entry.
Once the gate becomes active, its normal gate ref or head branch keeps it
reachable. Closing the pull request or changing its gate inputs removes the
failed-gate ref. Startup recovery repairs both namespaces from their owning
entries.

One task (`autoland::start`, started by `serve` after the poller) advances queues
for all repositories, including those with `auto_land` off. It subscribes to
`pull`, `check`, `repo`, and `upstream` events and sweeps the affected repositories.
Start, every minute, and an event-stream `reset` sweep them all. Sweeps do not
overlap. `merge_pull` and `publish_pull` recheck the repository, queue, tips,
checks, and approvals under their locks before acting.

With `auto_land` on, a passed first entry merges locally or publishes as its
author. Automatic publication happens only while the pull request has no
`upstream` record, checked again under its lock. Once published, including a
failed or stopped publication, another publication is the owner's action.
`upstream.auto` records who published it. Queues and new gates continue for
already published pull requests.

Each gate that passes with `auto_land` on and an upstream records `notify: true`.
Once the queue's next action is `owner_merge_host` (`landing::first`, the same
rule the GUI shows: the head is on the host, sending is not stopped, and every
host check of it succeeded), the owner's webhook receives `{"text": …}` saying
the gate passed and the pull request is ready to merge there. The host
registers its checks some time after a push or an event of the pull request
(its creation, an edit Branvil sends, its reopening), and
a workflow run creates a job's check run only once it starts the job (after the
jobs it needs, its concurrency group, or an approval), so the checks read are
all there are only when both hold: the poller has read the host after
`[upstream] checks_registration_secs` (60 s by default) passed since the last
event (`Published.checks_registered_by`, cleared by that read; a failed read
clears nothing), and at that read every GitHub Actions workflow run of the head had
finished, and so had every other app's check suite of it that has check runs
(`Published.checks_settled`; how the host says so is the provider's). This includes a
new gate for an already published head. `Landing.notified_at` records a `2xx`
answer for that gate; a later pass has its own notification. Pending notices
are retried even if automatic landing is later turned off. The URL is kept in
`secrets/webhook.url` (`0600`), never returned or backed up. Delivery is at least
once: a crash before the write can repeat it. Failures are kept in memory in
`GET /admin/webhook`'s `problem`, without the URL. A send times out after 10
seconds, and a sweep stops sending after one failure.

## Backups

`POST /api/v1/admin/backup` takes the repository-creation lock and every
repository lock (in id order), copies the database as of one read transaction
(`Store::export_to`), and writes a `git bundle --all` of each repository with
its own Git runner (Smart HTTP children may hold the shared Git permits while
their post-receive waits for those locks). Then it releases the locks and
copies CI logs and artifacts, tickets' attachments, and the configuration, and
writes a manifest with checksums. `branvil restore` is offline. See `docs/backup-restore.md`.

## Events

`GET /api/v1/events` streams `event: change` with `{kind, path, id}` (kinds include `upstream`: the
repository) from a
broadcast channel of 256 entries; a subscriber that falls behind receives
`event: reset` and refetches. Keep-alive comments keep idle connections open.
The landing task is a subscriber of the same channel inside the service
("Landing queues and automatic landing").

## Entering the GUI

There is one person, the owner, and no sign-in screen. The owner's token text
is kept in `<data>/secrets/owner.token`; whoever can read that file (the Hub
machine's OS user) is the owner. A browser gets a session only through an
entry link:

1. A client holding the owner's token calls `POST /api/v1/session/links` and
   receives `{base_url}/-/enter#<code>` (single use, 60 s, kept in memory as a
   hash; the code is in the fragment, so it is in no request line, log, or
   Referer).
2. The browser opens the link; `/-/enter` removes the code from the address bar
   and exchanges it at `POST /api/v1/session` for the session cookie
   (`HttpOnly`, `SameSite=Strict`) and its CSRF value.

`POST /api/v1/session/links` is the only way a GUI session starts, for every
client present and future. On the Hub machine the client is `branvil open`.
Agents never enter the GUI; they use their own tokens with the API and Git.

### Local apps: an owner token through a consent screen

Other machines reach Branvil at the same base URL through OpenSSH forwarding but
cannot read `owner.token`. A local app there (for example GuildBotics Desktop,
which sets Branvil up for its members and opens the GUI) gets an owner token of
its own instead, through the consent screen (`docs/api.md`, "Local apps"):

1. The app opens the browser at `{base}/-/authorize` with its name, a loopback
   `redirect_uri`, a `state`, and a PKCE `S256` challenge.
2. The owner, in the GUI (entering first with `branvil open` when the browser
   has no session; the page is returned to afterwards), allows it. Branvil
   makes a single-use 60-second code bound to the app's name, the
   `redirect_uri`, and the challenge (kept in memory as a hash, like entry-link
   codes) and sends the browser to the app's loopback address with it.
3. The app exchanges the code and its verifier at `POST /api/v1/apps/token`
   for an owner token labelled `app:<client>`; a previous token of that label
   is revoked in the same write.

With that token the app uses the admin API (members, tokens, accounts,
workspaces) and, to open the GUI, the entry-link endpoint above, exactly as
`branvil open` does. Branvil knows nothing of the app beyond its display
name; where the app keeps the token is its own matter. The owner revokes it
under Settings → Members → owner → Tokens, like any token; replacing
`owner.token` does not revoke it.

An app shows the GUI in its own top-level window or the system browser, never
in an iframe: Branvil forbids framing (`X-Frame-Options: DENY`,
`frame-ancestors 'none'`), and its `SameSite=Strict` cookie would not be sent
from a cross-site frame.

## GUI files

A workspace's task flow is at `/{workspace}`, its task board at
`/{workspace}/-/board`, and its pull requests at `/{workspace}/-/pulls`.
`/` and workspace creation always open the task flow. The display preference
only hides the board's sidebar link; direct board URLs remain usable. Switching
workspaces keeps the task flow, pull requests, or settings, and the board when
its sidebar link is shown; other pages lead to the task flow. The removed
`/{workspace}/-/relations` path is not redirected.

The GUI is built with Mantine and looks like GuildBotics Desktop: the same
Mantine version and theme (`web/src/theme.ts`), and GuildBotics' design tokens
and component overrides copied into `web/src/styles.css` (kept identical), so
it can sit beside or inside Desktop. Its pages: a sidebar (the workspace, its
task flow, task board and pull requests, settings), the task board (lanes as tall as the window, a
ticket added at the foot of a lane, a card marked when a blocker is open,
its lane and assignee changed from the card, the ticket made interactive or
not and its URL copied from the card's menu, which says that it copied and
then closes),
the task flow (the board's cards laid out left to right by their blockers, a
column's tickets in progress first, then ready, new, and done, each lane in the
board's order, a ticket pushed down, that order kept, to stand level with the
topmost ticket it waits for,
children inside their parent once it is opened, a parent coloured as one even
while none of its children is shown, lines drawn in SVG over a CSS
layout; text, assignee, and label filters hide nonmatching tickets and their
lines, keeping only matching tickets and their ancestors (shown faintly for
context), automatically opening those ancestors and the frame of new tickets
to reveal matches; opening or closing frames during filtering is temporary,
and clearing the filters restores the usual expansion state;
successfully creating a ticket clears those filters so its card can be
revealed, including when its relation is refused; cancelling or a failed
creation keeps the filters;
the new tickets without relations in one frame that looks like an open
parent's, its header in another colour, laid out where a new ticket would be;
only a ticket without relations (shown or not) is new: dragging a card onto
another makes it a child and a line drawn from a card's handle makes the other
card wait for it, and either makes the new tickets it relates ready; a new
ticket dropped on the background (out of its parent or the frame) is made
ready; the undo of a change of relations also puts them back in new; one
without relations dropped onto the frame is made new; near the top or bottom
of a card of its own lane a card is put there on the board; a ticket added
from the header is new, one added from a card's menu as its parent, child, or
next ticket ready (that menu also makes the ticket interactive or not, and
copies the ticket's URL and says that it copied), and either is scrolled to
once shown;
`web/src/views/relations/`), a ticket (body and comments, with comments editable
and permanently deletable by their author or the owner after confirmation; its lane, labels,
upstream issue, relations to other tickets, and assignee; from the board and
the task flow it opens in a drawer over them, whose URL stays, with a button
that copies the ticket's URL, and a ticket linked from the drawer replaces
it), a pull request (body, conversation,
checks, merge or publish; files and a check's job and log in drawers), and
settings (the shown workspace with its repositories, and Branvil's members, CI,
and notifications).
On the ticket surfaces (the new-ticket modal, the body editor, and the comment
compose and edit forms), an image is pasted, dropped on the field, or chosen
with "Attach images"; it is uploaded (`POST /repos/{repo}/attachments`) and
written at the cursor as `![name](url)`, with a placeholder while it uploads.
Save, Comment, and Create wait for it, and a refused image (not PNG, JPEG, GIF,
or WebP, or over 10 MiB) is explained under the field. Pull request bodies and
comments attach none: only tickets keep an attachment.
Displayed commit IDs link to that repository's permanent commit page, including
review revisions, check results and reused checks, merge heads and bases, and
upstream tips. Reused checks keep a separate link to their source job.
The pull request list shows the server's review states with the same avatars
and badges as its detail page. It merges repository pages by newest update first,
retaining unused rows for the next page, and adds 30 rows per "Load more" action.
Text search filters the loaded rows; an empty filtered page says when older
pages remain available to continue searching.
The merge refills empty repository pages before selecting the next row, so older
rows from one repository cannot appear ahead of unread newer rows from another.
Pull, repository, and upstream events refetch the loaded pages from the start.
The page header carries default-branch CI: a summary counts repositories by CI
status (the selected repository's tip shows its status alone), and opening it
lists repository links, default branches, status badges and commit links in a
drawer, or, for a selected repository, its tip's jobs, which open the existing
job drawer. The first 30 repositories are read, with any omission shown. The
queries stay active while the drawer is closed and use the same event
invalidation as the other pages.
The list sizes state and CI columns to their contents, wraps reviewer avatars,
and scrolls horizontally within its own frame when the columns need more room.
It names repositories without the workspace prefix.
Where an open pull request is on its way into its base is decided once
(`whereabouts` in `web/src/views/pull/LandingStatus.tsx`) from its queue entry,
the required checks on its head, its approvals, the statements awaiting its
author's answer (each linked on the page) and draft state: the stops are
requirements, queue, gate, the upstream host (with an upstream only), merge and
base, and at the current stop Branvil, the host or CI is working, a person has
a turn, or the pull request left the queue there. A queue entry for an older
head, or a departure the head or its author clears (a move, a draft, an
answer), puts it back at
the requirements. The list shows this as one badge per open pull request (its
title names every missing requirement, the departure reason, or who goes
first); the pull request's page shows it as a card under the header with every
stop, one line saying the state and whose turn it is, and only what the current
stop needs: missing requirements, the pull request that enters the gate first,
gate progress, the host's pull request when the owner acts there, how many wait
behind, or the reason it left and how to return. Check details stay in the
check list, which marks required checks.
The merge or publish card holds only the action. It stays disabled until the
required checks have succeeded on the displayed head (while checks are loading
or cannot be read, they remain unsatisfied), the approvals reach the
repository's required approvals, and the gate has passed at the front of the
queue; the reviewer badges and that count come from the review states the
server puts in the pull request. The server rechecks these conditions when the
action is submitted. For a stopped publication, the owner returns it to the
queue before publishing again after the gate passes. When the repository lands
automatically, the card says that it acts by itself, and a pull request it
published says so.
While a card is dragged, the box under the pointer scrolls when the pointer is
near its edge, faster the nearer (`web/src/dragScroll.ts`).
The service's state is not a page: the GUI warns at the top of every page only
when the page has lost its event stream for 5 seconds (it may be out of date
until the stream is back), CI cannot run, or an upstream cannot be followed.

`web/dist` is embedded with rust-embed (`allow_missing`, so the service builds
without it). Debug builds read the folder from disk, so a GUI built again
shows on reload; release builds, which `scripts/serve.sh` runs, carry the GUI
of their build. Every GET path that is not
`/api/…`, `/git/…`, or `/-/hooks/…` and not a file is answered with `index.html`, so
the web URLs of `api.md` open the single-page GUI. Every response carries
`Content-Security-Policy` (`script-src 'self'`, no inline scripts; `style-src
'self' 'unsafe-inline'`, because the GUI's components (Mantine, as in GuildBotics
Desktop) set their look through `style` attributes), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and
`Referrer-Policy: no-referrer`. No CORS headers are sent.

## Platforms

Built and tested on macOS 26 (arm64) with Git 2.54; the CI executor is
verified there with msb 0.6.17 (`docs/ci.md`). Linux and Windows are
unverified. The hook scripts are POSIX `sh` scripts (Git for Windows runs hooks
with its bundled `sh`, but this is untested), and the `secrets/` permissions
are set only on Unix.
