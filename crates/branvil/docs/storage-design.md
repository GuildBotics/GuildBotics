# Storage design

Branvil keeps metadata in one redb file (`<data>/branvil.redb`), Git objects in
bare repositories (`<data>/repos/<path>.git`), and CI logs and artifacts in
files under `<data>/ci/` (never in redb). Only the service process opens the database; the
GUI and the runner go through the HTTP API and GuildBotics through the data API
in the same process (the offline `guildbotics server hub-data` commands open it
only while the server is stopped, and redb's file lock refuses a second
opener).

This document derives the tables from [`port-mapping.md`](port-mapping.md):
for each record it says which operations read and write it, which index serves
which query, and what one write transaction covers. The stored records are not
the API shapes: views are assembled in `src/domain/` from records plus live Git
state (branch tips, diffs), and anything that depends on the caller
(`assignee=me`, `involves=me`) is computed at request time.

## redb settings

| Setting | Value | Why |
| --- | --- | --- |
| redb | 4.4.0 (stable API only; `experimental-api-5` is not enabled) | `Cargo.lock` |
| Durability | redb default (`Immediate`: a commit is on disk when `commit()` returns) | reviews, merges, and ref journal entries must survive a crash |
| Cache | `storage.cache_size_mib`, default 32 MiB | evaluation starting point; it is redb's page cache, not a process limit |
| Writers | one async semaphore permit in front of `begin_write` | at most one blocking thread waits on redb's writer lock |
| Readers | `storage.read_concurrency` (default 8) permits | bounded `spawn_blocking`; the async runtime never blocks on storage |

No transaction is held across Git, network, or user input: a request reads Git
first, then runs one short closure inside `Store::write`, which commits when the
closure returns `Ok` and aborts otherwise.

## Encoding

- Values are JSON (`serde_json`) of the structs in `src/model.rs`. Readability
  over compactness for now.
- Keys are order-preserving tuples (`store::Key`): a string element is its
  bytes with `00` escaped as `00 FF` and a `00 01` terminator; an integer is 8
  bytes big-endian. Element-wise order equals byte order, a prefix of elements
  is a byte prefix (prefix scans), and `("a","b|c")` cannot collide with
  `("a|b","c")`. Numbers never compare as strings (`10` sorts after `9`).
- Paths and member names are case-preserving in records and case-folded
  (`to_lowercase`) in keys, so `Acme/Demo` and `acme/demo` cannot both exist.
- Workspaces and repositories have internal ids (`ws_…`, `repo_…`,
  ULID-based). Keys use ids, never paths; URLs are built from paths and
  the configured `base_url` at response time, so changing `base_url` changes
  every URL Branvil returns (and breaks URLs recorded elsewhere: keep it fixed).
- Entry ids (`cmt_…`, `rev_…`, `ilc_…`, `rct_…`) are opaque; ordering inside an
  item comes from a service-wide sequence counter (`counters["entry"]`), not from
  the id.
- Timestamps are RFC 3339 UTC strings with milliseconds, so string order is
  time order (used in the closed-at and patrol orderings).

## Schema version

Raise the version by at most one between successive verified commits. While a
schema raise is still unverified, add further format changes to that same
step's upgrade instead of raising the version again. The `schema-raise` CI job
compares the checkout with `BRANVIL_VERIFIED_SHA` and also requires `upgrade.rs`
to remain unchanged when the version stays at the verified version. It skips
the comparison when no verified commit exists, it is not an ancestor of the
checkout, or it predates the guard script.

`meta["schema_version"]` is written on creation (currently `39`). The current
format includes per-pull landing conditions, each merged pull request's original
reviewed head distinct from its merge commit, and per-head notification and
automatic failure state. CI runs hold their commit's pipeline and tree;
attempts hold reuse sources and cache keys. Runs retain the protected branches that
requested them. Repair tickets retain their undelivered creation notice.
Runs also hold the input digest of each job with `paths`, and attempts the one
of their job when claimed. `ci_verifications` is keyed by repository id and stores
`sha`, `base`, `state` (`running`, `passed`, `failed`), `started_at`, `finished_at`,
`failed_jobs` (name, last attempt id or null, details), `failing_since`, and
`last_verified` (SHA and time). Attempts store optional `verified_sha`; job
snapshots store the opt-in boolean of the same name. `ci_narrowings` is keyed by
(repository id, base SHA, job name), holding the failed SHA, first-parent commits,
per-commit attempt IDs and outcomes, history readiness, pending probe, conclusion and optional
boundary/reason, and start/finish timestamps. `ix_ci_narrowing` contains only
`running` rows with the same key and a primary-key value. Admin check/reindex
validate/rebuild it. The 32 → 33 upgrade creates these empty tables without
changing existing records. Attempts support purpose `probe`.
Tickets and conversation comments store their resolved `mentions` lists.
The 19 → 20 upgrade resolves existing bodies against the stored members,
writes those lists, and rebuilds the derived indexes in the upgrade transaction.
Images attached to tickets are recorded in `attachments`; the 23 → 24
upgrade indexes the attachment ids the kept ticket bodies and comments refer
to (`ix_attachment_refs`).

A database of any other version is refused at open. The previous version is
upgraded by `guildbotics server hub-data upgrade` or `guildbotics server hub-data restore` (`src/upgrade.rs`, the
only code that reads previous shapes, replaced whenever the version rises).
The upgrade keeps a copy of the database as it was and sets `meta["unchecked"]`
in the transaction that applies it. The server refuses a marked database
until a passing `guildbotics server hub-data check` removes the mark (`docs/backup-restore.md`,
"Upgrading"). A database without the mark is not marked: introducing this
check did not change stored records or raise the version.

## Primary tables

| Table | Key | Value | Written by | Read by |
| --- | --- | --- | --- | --- |
| `meta` | `(name)` | schema version; `unchecked` while an upgrade has not passed `guildbotics server hub-data check` (absent otherwise) | open, upgrade, `admin check` | open, `serve`, `admin upgrade` |
| `counters` | `(name)` | `u64` | ticket numbers `ticket:<repo>`, PR numbers `pull:<repo>`, `entry` sequence | allocation in the same txn as the insert |
| `members` | `(fold(name))` | member, kind (`owner`, `agent`, `runner`), accounts on upstream hosts (provider, login; the token is a file), reviewers of the pull requests it opens | owner API, `init`, automatic runner | auth, kind checks, upstream |
| `tokens` | `(token id)` | member, label, SHA-256 of the secret, last use | token API | auth (by id, constant-time hash compare) |
| `workspaces` | `(workspace id)` | name | workspace API | repository creation, cards, closed |
| `repos` | `(repo id)` | path, workspace id, default branch, protected branches, required checks, required approvals, `auto_land`, `hold_while_failing` (boolean, defaults to false on creation and upgrade), `ci_network`, `responder` (explicit agent name or null for automatic), labels, upstream (provider, repository), revision | repo API | everything under a repository |
| `tickets` | `(repo id, n)` | ticket incl. its workspace id, its one assignee with `assigned_at` (or none), labels, whether it is interactive, upstream issue (or none), lane/rank on the workspace's board, its parent (or none) and the tickets it is blocked by, as (repo id, n), state, revision | ticket API, merge | `read(tickets)`, candidates |
| `ticket_moves` | `(repo id, n)` of a ticket moved to another repository | the `(repo id, n)` it moved to | ticket move | ticket lookups by number or URL, which follow each move (numbers are never reused, so the chain ends) |
| `comments` | `("ticket", repo id, n, seq)` / `("pull", repo id, n, seq)` | comment with its reactions | comment and reaction API, landing conflicts and failed host checks | comments, cards, activity |
| `attachments` | `(repo id, attachment id)` | an image of the tickets of the repository's workspace (a moved ticket keeps the ones of its old repository): name, media type read from its bytes, size, uploader, time (the bytes are a file) | upload API, the attachment sweep | delivery, a ticket's attachments, the sweep |
| `pulls` | `(repo id, n)` | pull request record (links, reviewers, reviewed_by, draft, state, original merged head and base, merge commit, and once published: the host's pull request, what was last sent to it, the host's checks of the pushed head and whether it may still add some, whether Branvil published it) | PR API, merge, publish, the upstream poller, push recording (`updated_at`) | PR reads, patrol, the upstream poller |
| `landing` | `(repo id, n)` | checked head/base SHA, conflicts, told problems, notified_at, automatic error | sweep, merge/publish, webhook acknowledgment | PR conditions, next action, patrol |
| `reviews` | `(repo id, n, seq)` | review with reactions | review API | reviews, activity |
| `threads` | `(repo id, n, root seq)` | path, line, side, commit, resolved | inline-comment API | threads |
| `inline_comments` | `(repo id, n, root seq, seq)` | thread comment with reactions | inline/reply API | thread comments (contiguous per thread) |
| `ref_snapshots` | `(repo id, refname)` | last recorded object | `refs::record_refs` | push recording, startup rescan |
| `ref_journal` | `(journal id)` | repo, ref, expected old/new, operation | merge (before `update-ref`) | completion, startup recovery |
| `idempotency` | `(fold(member), key)` | request hash, status, response body, time | API middleware | API middleware |
| `ci_narrowings` | `(repo id, base sha, job name)` | failed SHA, first-parent history, observations and attempt IDs, pending probe, conclusion, boundary/reason, start and finish times | failed verification, check event, reconciler | repair hints |
| `ci_runs` | `(repo id, sha)` | branches that want CI of the commit, tree (unset only for upgraded runs with missing commits), the commit's pipeline (absent / invalid + message / job definitions), cache scope, waiting, superseded by, protected-branch history, input digest of each job with `paths` (`inputs`: SHA-256 of `git diff-tree -r -z --no-renames --raw <empty tree> <sha> -- <the job's paths>`, replaced at every registration; kept without Git while the pipeline is the same) | ref recording (`Ci::on_ref_changes`), PR open/reopen, reconciler, queue admission | checks, claims, retries |
| `ci_attempts` | `(attempt id)` | kind (`check`, `probe`, or `historical`), job definition (snapshot), image configuration digest, actual source attempt if reused, cache key of an actual execution, reuse permission (manual retries execute), number, queue sequence, status, conclusion, details, SHA-256 of the lease, lease expiry, cancel requested, runner, cache scope, network, times, interrupted/superseded flags, artifact names and sizes, files pruned, the run's input digest of the job when claimed (`inputs`, none for a job without `paths`) | registration, claim, heartbeat, finish, cancel, retry, sweeps | checks, job API, runner API, artifacts |

Each comment, review, and inline comment is its own record, so adding one never
rewrites a history; a reaction rewrites only its target entry.

Landing conditions are computed for each open pull request independently by
`landing::blockers`: `draft`, `already_in_base` (including an empty head),
`conflict` (with `files`), `publish_requirements`, `head_checks`, `approvals`,
`unanswered`, and `host_failed` (with `jobs`). A stale recorded head/base pair
adds `checking` until the sweep refreshes it. A behind head can land without
merging the base into it when it does not conflict. Required checks accept
service path skips. No gate commits or landing queues are created.

Merge and publish re-evaluate the conditions on the current head and base under
the repository lock; blockers return `409 not_mergeable` with their codes and
messages. Local merges create the merge commit using the local merge policy;
publishing sends the original reviewed head. Integration of independently
landed changes is checked by main verification after merging.

The record retains `repo`, `number`, `head_sha`, `base_sha`, `conflicts`,
`told`, `notified_at`, and `error`. A new head clears told problems, notification
time and automatic error. Closing or merging deletes the record. The sweep
refreshes conflict files when either tip changes and comments as `branvil`
once per head for conflicts and once per failed host job. These comments have
no `meta` and await the author's answer. All consumers use the same blockers.

## Derived indexes

Every index entry of a record comes from one function in `src/model.rs`
(`ticket_index`, `pull_index`, …), used both when the record is written
(`put_indexed` diffs the old and new entries in the same transaction) and by
`guildbotics server hub-data check` / `guildbotics server hub-data reindex`. The two cannot drift apart.

| Index | Key → value | Serves (port-mapping) |
| --- | --- | --- |
| `ix_paths` | `("workspace"|"repo", fold(name or path))` → id | name and path lookup; unique names and paths |
| `ix_member_tokens` | `(member, token id)` | token list/revoke |
| `ix_cards` | `(workspace id, lane index, rank, repo id, n)` | `candidates` (`GET workspaces/{w}/board/cards?lane=…`), card order, `place` neighbours |
| `ix_ticket_labels` | `(repo id, label, n)` | `?label=` lists; consistency check for concurrent label changes |
| `ix_ticket_mentions` | `(fold(member), repo id, n, source)`; source `0` for the ticket body, comment sequence otherwise | workspace `tickets?mentions=me`; each source derives its own entries in `model.rs`, so editing or deleting one never removes another source's mention |
| `ix_ticket_refs` | `(repo id, n, "parent"|"blocked_by", repo id, n)`: the ticket named, then the one naming it | a ticket's `children` and `blocks`; the open-children check of a completion; the "wait on each other" check of a new relation |
| `ix_attachment_refs` | `(repo id, attachment id, n, source)`; source `0` for the ticket body, comment sequence otherwise | the attachment sweep keeps an attachment that has one; like mentions, each source derives its own entries from the attachment ids in its text (`model::attachment_ids`) |
| `ix_closed_tickets` | `(workspace id, closed_at, repo id, n)` | `closed_since` for tickets |
| `ix_entries` | `(entry id)` → `{kind, owner, record key}` | reactions, replies: an id is used only with its owning item (`owner` must match) |
| `ix_open_pulls` | `(repo id, head, base)` → number | unique open (head, base) on create and reopen; `open_pull_requests`; pushes → PR activity; the owner's `GET /pulls?involves=me` |
| `ix_involved` | `(member, repo id, n)` for open PRs (author, reviewers, reviewed_by) | `pull_request_candidates` (`GET /pulls?involves=me`) |
| `ix_ticket_pulls` | `(repo id, n, repo id, pr n)` | `ticket_pull_requests`, card `pull_requests` |
| `ix_closed_pulls` | `(repo id, closed_at, n)` for closed PRs | `closed_since` for pull requests |
| `ix_idempotency_time` | `(created_at, member, key)` | expiring idempotency keys after 24 hours |
| `ix_ci_sha` | `(repo id, sha, job, attempt number)` → attempt id | checks (latest attempt per job), next attempt number, artifacts of a commit |
| `ix_ci_pending` | `(queue sequence)` → attempt id, queued attempts only | claim (oldest first), queue length and bound, no-runner sweep |
| `ix_ci_active` | `(attempt id)`, in-progress attempts only | lease-expiry sweep (also the orphans found after a restart) |
| `ix_ci_waiting` | `(run sequence)` → run key, runs that did not fit the queue | admission in order when room frees up |
| `ix_repair_unnotified` | `(repo id, ticket number)` | repair tickets with a non-null notice, including closed tickets; maintained by `model::ticket_index`, rebuilt by `admin reindex` |
| `ix_ci_tree` | `(repo id, tree, sha)` → run key, runs with a known tree | finding successful actual executions with the same tree at claim |
| `ix_ci_inputs` | `(repo id, job, input digest, attempt id)` → attempt id, completed successful attempts that were not reused, have an input digest, and keep their files | finding the successful actual executions of a job's inputs in one range read |

Cards read only the requested lanes' ranges of one workspace's `ix_cards` (the
`done` lane keeps every finished ticket, and candidates ask for `ready` and
`in_progress`); the assignee filter is applied to the tickets of those lanes.
Placing a card (create, move, merge closing a ticket) reads only
the two index entries next to the new position, never the whole lane. Ticket
lists scan one repository's range (or one label's index range). Mention queries
read the repository records for the workspace and scan only the requesting
member's mention ranges in those repositories. Each source remains a separate
index entry; the reader groups consecutive entries of a ticket, including
the ticket named by the preceding page's cursor. `closed_since`
reads the repository records to find the workspace's repositories, whose pull
requests it lists (GuildBotics calls it to import the board's finished work as
activity, not on its patrol path). No hot path scans a whole table. Lists follow signed cursors: the cursor is the last position
(record key, or sort key for lists sorted in memory) plus an HMAC-SHA256 tag
over the collection, the caller, and every query condition, keyed by 32
random bytes made at each start: a restart ends the cursors handed out before
(`invalid_cursor`), which clients recover from by starting the list again.

## Transaction boundaries (atomicity)

| Operation | One write transaction covers |
| --- | --- |
| create ticket | repository re-read, label check, number allocation, insert, card placement, indexes |
| update ticket | conditional revision check, label add/remove against the stored set, state change, indexes (concurrent label changes are serialized by redb's single writer, so none is lost) |
| delete ticket (owner only) | read ticket, refuse any incoming parent/blocker relation or PR link or upstream issue, then remove the ticket and all its comments with their derived indexes in the same transaction; keep the `ticket:<repo>` counter so numbers are never reused |
| create PR | open (head, base) lookup, link validation, number allocation, insert, indexes (concurrent creates make one PR) |
| review / inline comment | insert + PR activity + involvement; the head was read from Git just before, and the write is linearized at that read |
| reaction | read-modify-write of the target entry, after checking the entry belongs to the item |
| merge (DB side) | PR merged/closed with original reviewed head and merge commit, landing entry left, closed-at index, `closes` tickets closed and moved to `done`, journal entry removed |
| landing transition | landing record and any conflict or failed host-check comment; does not update PR activity |
| publication result | upstream record |
| landing notification acknowledgment | `notified_at` only if the entry is still the same head |
| push recording | ref snapshot diff, `updated_at` of open PRs whose head moved |
| upstream poll of a pull request | what was sent and read, and a close or reopen the host made; a merge on the host is applied with the merge's own effects (above) in the same transaction |
| CI registration (after push recording, same repository lock; the pipeline was read from Git before) | run created or the branch added to it, queue bound check, every job's attempt queued (or the run marked waiting) |
| CI supersession | the branch removed from the old commit's run; when none is left, its queued attempts cancelled and running ones marked cancel-requested |
| claim | oldest pending attempt → in progress, new lease hash and expiry, pending entry removed, active entry added |
| heartbeat / finish / artifact record | lease checked (current and unexpired) and the attempt updated in the same transaction; `409` writes nothing |
| lease sweep | each expired attempt concluded as interrupted and, the first time, its re-queued successor inserted |

## Files, retention, backups

- Git objects live only in Git. Repositories are never deleted by the API
  (there is no delete endpoint yet).
- The data directory (0700), `ci/logs`, `ci/artifacts`, `attachments`, and
  `secrets/` (0700, for the owner's token alone) are created at start.
- Tickets' attachments: `attachments/<attachment id>` (written to a temporary
  name, synced, renamed, and only then recorded in `attachments`). Deleting a
  ticket removes its body/comment attachment references with the other indexes;
  it does not delete shared repository attachments. Unreferenced images follow
  the same sweep as after editing a body or deleting a comment.
  At start and every hour the sweep deletes, each in a transaction that checks
  `ix_attachment_refs`, the attachments no ticket body or ticket comment has
  referred to for 24 hours after their upload, and the records whose file is
  gone; then the files that have had no record for 24 hours (a crash between
  writing and recording, an upload after a backup's database copy).
- CI: `ci/logs/<attempt id>.log` (appended while the job runs, at most
  `ci.log_max_mib`) and `ci/artifacts/<attempt id>/<name>.zip` (written to a
  temporary name, synced, renamed, and only then recorded in `ci_attempts`).
  Logs and artifacts of attempts finished more than `ci.retention_days` ago are
  deleted hourly and the attempt is marked `files_pruned` (its record and
  conclusion stay). At start, files that belong to no attempt (a crash between
  writing and recording, a restored backup taken mid-job) are deleted. Run and
  attempt records are kept; they are small (one per commit and job try).
- The attachment sweep and CI retention delete files only while no backup is
  between its database copy and its copy of the files; meanwhile they leave
  them to their next pass (`App::file_deletions`, `docs/backup-restore.md`).
- Idempotency keys expire after 24 hours; finished journal entries are deleted
  when they complete.
- Members' upstream credentials and the webhook's URL are in the service's
  vault (`src/vault.rs`), not in the data directory; a credential is deleted
  with its account.
- Backups and restores: `docs/backup-restore.md`. The token hashes in the
  database are not secrets by themselves; `secrets/` is not backed up.

## Repair records (schema 30)

`Ticket.repair` is null for ordinary tickets or `{jobs: [...], reported: sha, notice: text or null}` for
repair tickets. `REPAIR_PENDING` (`repair_pending`) keys are `(repo id, sha)` and
values contain the repository, SHA and failed jobs (name, optional attempt ID,
details). `IX_OPEN_REPAIRS` (`ix_open_repairs`) marks `(repo id, ticket number)`
for open tickets with a repair marker. `model::ticket_index` maintains it and
`admin reindex` rebuilds it; the oldest entry is the repository's open repair.
Ordinary tickets have `repair: null`. The 29 → 30 upgrade adds `repair: null`
to all tickets; `repair_pending` and `ix_open_repairs` start empty.
There is no parallel reader for older schemas.

Repository records (`repos`) include `post_merge_jobs` (job names). Run selections
(`ci_runs`) snapshot `post_merge_jobs` alongside bases and skipped jobs, so changing
the setting invalidates selection. Service skips in `ci_attempts.path_skip` carry
`reason` (`paths` or `post_merge`), `paths`, and `bases`. Both reasons have the same
queue, reuse, retention, and landing semantics.

The 33 → 34 upgrade removes run failure-delivery fields, attempt recovery-delivery
fields and `ix_ci_unnotified` / `ix_ci_recovered`. Existing repair tickets get
`notice: null`; the new `ix_repair_unnotified` starts empty.

The 34 → 35 upgrade adds `hold_while_failing: false` to every repository.

The 35 → 36 upgrade revokes the tokens GuildBotics kept: those labelled
`guildbotics` and those labelled `app:<client>`.
