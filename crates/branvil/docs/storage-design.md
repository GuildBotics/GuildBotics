# Storage design

Branvil keeps metadata in one redb file (`<data>/branvil.redb`), Git objects in
bare repositories (`<data>/repos/<path>.git`), and CI logs and artifacts in
files under `<data>/ci/` (never in redb). Only the service process opens the database; the
GUI, the GuildBotics provider, and the CLI go through the HTTP API (the offline
`branvil init` / `branvil admin` commands open it only while the service is
stopped, and redb's file lock refuses a second opener).

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

`meta["schema_version"]` is written on creation (currently `24`). The current
format includes persistent landing queues, each merged pull request's original
reviewed head distinct from its merge commit, and per-gate notification and
publication-resume state. CI runs hold their commit's pipeline and tree;
attempts hold reuse sources and cache keys. Runs retain the protected branches that
requested them, whether a required failure awaits notification, and its
successful webhook delivery time.
Tickets and conversation comments store their resolved `mentions` lists.
The 19 → 20 upgrade resolves existing bodies against the stored members,
writes those lists, and rebuilds the derived indexes in the upgrade transaction.
Images attached to tickets are recorded in `attachments`; the 23 → 24
upgrade indexes the attachment ids the kept ticket bodies and comments refer
to (`ix_attachment_refs`).

A database of any other version is refused at open. The previous version is
upgraded by `branvil admin upgrade` or `branvil restore` (`src/upgrade.rs`, the
only code that reads previous shapes, replaced whenever the version rises).
The upgrade keeps a copy of the database as it was and sets `meta["unchecked"]`
in the transaction that applies it. `branvil serve` refuses a marked database
until a passing `branvil admin check` removes the mark (`docs/backup-restore.md`,
"Upgrading"). A database without the mark is not marked: introducing this
check did not change stored records or raise the version.

## Primary tables

| Table | Key | Value | Written by | Read by |
| --- | --- | --- | --- | --- |
| `meta` | `(name)` | schema version; `unchecked` while an upgrade has not passed `branvil admin check` (absent otherwise) | open, upgrade, `admin check` | open, `serve`, `admin upgrade` |
| `counters` | `(name)` | `u64` | ticket numbers `ticket:<repo>`, PR numbers `pull:<repo>`, `entry` sequence, per-repository landing order `landing:<repo>` | allocation in the same txn as the insert |
| `members` | `(fold(name))` | member, kind (`owner`, `agent`, `runner`), accounts on upstream hosts (provider, login; the token is a file), reviewers of the pull requests it opens | owner API, `init`, automatic runner | auth, kind checks, upstream |
| `tokens` | `(token id)` | member, label, SHA-256 of the secret, last use | token API | auth (by id, constant-time hash compare) |
| `sessions` | `(sha256(session id))` | member, CSRF secret, expiry | entry-code exchange (expired rows pruned there); all deleted when the owner's token file is replaced | cookie auth |
| `workspaces` | `(workspace id)` | name | workspace API | repository creation, cards, closed |
| `repos` | `(repo id)` | path, workspace id, default branch, protected branches, required checks, required approvals, `auto_land`, `ci_network`, labels, upstream (provider, repository), revision | repo API | everything under a repository |
| `tickets` | `(repo id, n)` | ticket incl. its workspace id, its one assignee with `assigned_at` (or none), labels, whether it is interactive, upstream issue (or none), lane/rank on the workspace's board, its parent (or none) and the tickets it is blocked by, as (repo id, n), state, revision | ticket API, merge | `read(tickets)`, candidates |
| `comments` | `("ticket", repo id, n, seq)` / `("pull", repo id, n, seq)` | comment with its reactions | comment and reaction API, landing gate failures | comments, cards, activity |
| `attachments` | `(repo id, attachment id)` | an image of the repository's tickets: name, media type read from its bytes, size, uploader, time (the bytes are a file) | upload API, the attachment sweep | delivery, a ticket's attachments, the sweep |
| `pulls` | `(repo id, n)` | pull request record (links, reviewers, reviewed_by, draft, state, original merged head and base, merge commit, and once published: the host's pull request, what was last sent to it, the host's checks of the pushed head and whether it may still add some, whether Branvil published it) | PR API, merge, publish, the upstream poller, push recording (`updated_at`) | PR reads, patrol, the upstream poller |
| `landing` | `(repo id, n)` | base branch, admission time/order, state/time, gate SHA and its base/head inputs, departure reason with jobs/files, notification intent/time, stopped-publication resume intent | landing reconciliation, merge, owner resume/publish, webhook acknowledgment | queue and PR views, gate checks, merge/publish preconditions, CI gate ownership, webhook |
| `reviews` | `(repo id, n, seq)` | review with reactions | review API | reviews, activity |
| `threads` | `(repo id, n, root seq)` | path, line, side, commit, resolved | inline-comment API | threads |
| `inline_comments` | `(repo id, n, root seq, seq)` | thread comment with reactions | inline/reply API | thread comments (contiguous per thread) |
| `ref_snapshots` | `(repo id, refname)` | last recorded object | `refs::record_refs` | push recording, startup rescan |
| `ref_journal` | `(journal id)` | repo, ref, expected old/new, operation | merge (before `update-ref`) | completion, startup recovery |
| `idempotency` | `(fold(member), key)` | request hash, status, response body, time | API middleware | API middleware |
| `ci_runs` | `(repo id, sha)` | branches or current gates that want CI of the commit, tree (unset only for upgraded runs with missing commits), the commit's pipeline (absent / invalid + message / job definitions), cache scope, waiting, superseded by, protected-branch history, pending failure notice, failure delivery time | ref recording (`Ci::on_ref_changes`), PR open/reopen, reconciler, queue admission | checks, claims, retries |
| `ci_attempts` | `(attempt id)` | job definition (snapshot), image configuration digest, actual source attempt if reused, cache key of an actual execution, reuse permission (manual retries execute), number, queue sequence, status, conclusion, details, SHA-256 of the lease, lease expiry, cancel requested, runner, cache scope, network, times, interrupted/superseded flags, artifact names and sizes, files pruned | registration, claim, heartbeat, finish, cancel, retry, sweeps | checks, job API, runner API, artifacts |

Each comment, review, and inline comment is its own record, so adding one never
rewrites a history; a reaction rewrites only its target entry.

Each pull request has at most one `landing` row. A repository-prefix scan sorts
active rows by their stored `sequence` and groups them by base; departed rows
remain available for the failure reason, gate checks, and retries. Rejoining
replaces the row with a new admission time and sequence. Advancing base alone
keeps active rows' order and admission times; the first rebuilds its gate in
place. These writes leave `Pull.revision` and `Pull.updated_at` unchanged. Gate failures and conflicts
write their reserved-author `branvil` comment in the transaction that records
the departure. The next ref update removes the gate ref; ref recording and CI
reconciliation supersede work and repair interrupted registration. A failed
gate remains reachable through `refs/branvil/failed-gate/<number>` while the
open pull request retains those gate inputs for retry. A requeued retry keeps
that ref until its normal gate ref or head branch takes over; closing the pull
request or changing the inputs removes it. Startup repairs both namespaces from their
owning entries.


## Derived indexes

Every index entry of a record comes from one function in `src/model.rs`
(`ticket_index`, `pull_index`, …), used both when the record is written
(`put_indexed` diffs the old and new entries in the same transaction) and by
`branvil admin check` / `branvil admin reindex`. The two cannot drift apart.

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
| `ix_ci_unnotified` | `(repo id, sha)` → run key | protected runs with a final required-check failure awaiting webhook delivery; maintained in the same transaction as run/attempt changes |
| `ix_ci_tree` | `(repo id, tree, sha)` → run key, runs with a known tree | finding successful actual executions with the same tree at claim |

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
over the collection, the caller, and every query condition, keyed by
`<data>/secrets/cursor.key`.

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
| landing transition | queue entry and, for a gate failure or conflict, its service comment; does not update PR activity |
| stopped-publication resume | create a landing entry if absent and record resume intent; original upstream refusal remains until the owner publishes again |
| publication result | upstream record and clearing the landing entry's resume intent |
| gate notification acknowledgment | `notified_at` only if the entry is still the same passed gate and state time |
| push recording | ref snapshot diff, `updated_at` of open PRs whose head moved |
| upstream poll of a pull request | what was sent and read, and a close or reopen the host made; a merge on the host is applied with the merge's own effects (above) in the same transaction |
| CI registration (after push recording, same repository lock; the pipeline was read from Git before) | run created or the branch/gate added to it, queue bound check, every job's attempt queued (or the run marked waiting) |
| CI supersession | the branch/gate removed from the old commit's run; when none is left, its queued attempts cancelled and running ones marked cancel-requested |
| claim | oldest pending attempt → in progress, new lease hash and expiry, pending entry removed, active entry added |
| heartbeat / finish / artifact record | lease checked (current and unexpired) and the attempt updated in the same transaction; `409` writes nothing |
| lease sweep | each expired attempt concluded as interrupted and, the first time, its re-queued successor inserted |

## Files, retention, backups

- Git objects live only in Git. Repositories are never deleted by the API
  (there is no delete endpoint yet).
- The data directory (0700), `ci/logs`, `ci/artifacts`, `attachments`, and
  `secrets/` (0700; `cursor.key` 0600) are created at start.
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
- Sessions expire (`limits.session_hours`, default 720) and are pruned at
  the next entry-code exchange; idempotency keys after 24 hours; finished journal entries are deleted
  when they complete.
- Members' upstream credentials are `0600` files in `secrets/accounts/`,
  deleted with their account.
- Backups and restores: `docs/backup-restore.md`. The token hashes in the
  database are not secrets by themselves; `secrets/` is not backed up.
