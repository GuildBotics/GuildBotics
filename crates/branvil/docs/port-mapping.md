# GuildBotics ports → Branvil

Reference: GuildBotics `b7eff7266ed5d4ad12f5219d33bedaa030f5d113` (main after
GuildBotics#807). Sources read:
`guildbotics/runtime/{code_hosting_service,ticket_service,resources,port}.py`,
`guildbotics/entities/task.py`, `guildbotics/integrations/{provider,factory,repository_scope}.py`,
`guildbotics/integrations/{local,github}/`, `tests/guildbotics/integrations/contracts/`,
and the callers in `capabilities/member_repository.py`, `capabilities/member_git.py`,
`drivers/ticket_selector.py`, `capabilities/activity_events.py`,
`templates/commands/repository/*`.

The provider is `guildbotics/integrations/branvil/` in GuildBotics. It translates
port calls into the API of [`api.md`](api.md) and the answers into the port's
models. Branvil keeps the data; the provider keeps none. Selection rules that are
GuildBotics' own (workflow status, mentions, the review limit) are applied by the
provider to the raw activity Branvil returns, as the GitHub provider applies them
to GitHub's.

"Errors" below lists what the provider raises; every API failure maps as:
`404 → ServiceNotFound`, `422 unsupported → ServiceUnsupported`, any other
error response, a connection failure, or a timeout → `ServiceError` with a
message that names Branvil and the operation (never the response body verbatim
beyond the service's own `message`, which carries no secrets). A connection
failure says Branvil is not reachable at the base URL (the OpenSSH forwarding
service may be down; see `README.md`, "Several machines").

"contract" means GuildBotics' port contract tests
(`tests/guildbotics/integrations/contracts/`) for `branvil`, which run against
`BranvilDouble`, a Python double of this API; "smoke" means the operation is
driven against a real `branvil serve` by `scripts/branvil-integration.sh`, which
walks three paths (tickets and their relations, a push with a pull request and
its merge, checks deciding readiness; see "Checking with GuildBotics" in the
README); "provider unit tests on the double" are GuildBotics'
`tests/guildbotics/integrations/test_branvil_*.py` against `BranvilDouble`.

## Shared rules

| Rule | Where | How |
| --- | --- | --- |
| Write scope | provider | The scope is the configured workspace (`project.services.*.workspace`). Every write (and `check_push`) calls `repository_scope.check_path(workspace, <repo path>, provider="branvil")` before any request. The repository of a ticket or a pull request is parsed from its URL. |
| URL ownership | provider | A URL is Branvil's only when it starts with the configured `base_url` and matches the web URL shapes of `api.md`. A ticket URL of a pull request (and the reverse) is `ServiceNotFound`; a URL of another service is `ServiceNotFound` for item operations and `ServiceUnsupported` for `ticket_pull_requests`. |
| Entry ids | service | Endpoints take the item path and the entry id; an id of another item is `404`. |
| Author type | provider | `Message.ASSISTANT` when the entry's `author` equals the member's `branvil_username` (case-insensitive), `Message.USER` otherwise, `""` without an author. |
| Workflow status | provider + service | `comment(status=...)` sends `meta = status.payload()`; reads give `meta` back and `WorkflowStatus.from_payload(meta)` restores it. Branvil stores `meta` opaquely (4 KiB cap). |
| Reactions | service | Only `ack`/`agree`/`celebrate`/`support`; the provider refuses others with `ServiceUnsupported` before any request. |
| Pagination | provider + service | Port `continuation` is Branvil's `next_cursor` passed through; the service verifies it is bound to the same collection and conditions. Port `page_size` → `limit`. |

## CodeHostingService

| Operation | Input → output / errors | Reads | Writes | Index | Atomicity / races | API | Test | State |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `read(pull_requests)` | PR URL → one `PullRequest` | pull record, branch tips (Git) | — | `pulls(repo, number)` | tips read at request time | `GET pulls/{n}` | contract | done: smoke |
| `read(pull_request_comments)` | PR URL, page → `Comment` page | comments of the PR | — | `comments(target, seq)` | — | `GET pulls/{n}/comments` | contract | done |
| `read(pull_request_reviews)` | → `Review` page | reviews | — | `reviews(pull, seq)` | — | `GET pulls/{n}/reviews` | contract | done |
| `read(pull_request_files)` | → `FileChange` page (commentable lines computed by `integrations/diff_lines.file_change`) | Git diff | — | — | diff computed from tips read once | `GET pulls/{n}/files` | provider unit | done: provider unit tests on the double |
| `read(pull_request_threads)` / `review_thread_comments` | → `ReviewThread` / `ThreadComment` page | inline comments | — | `inline(pull, seq)` | — | `GET pulls/{n}/threads[/{id}/comments]` | provider unit | done: provider unit tests on the double |
| `read(pull_request_readiness)` | → `Readiness` | as `readiness` | — | — | — | — | contract | done |
| `read(ticket_pull_requests)` | ticket URL → `TicketPullRequest` page; foreign → `ServiceUnsupported`; PR URL → `ServiceNotFound` | ticket→PR links | — | `ticket_pulls(ticket, pull)` | — | `GET repos/{r}/tickets/{n}` (`pull_requests` via cards) / `GET repos/{r}/tickets/{n}/pulls` | contract | done: smoke |
| `read(dependency_alerts)` | repository path → `DependencyAlert` page, or one alert by `id`; the upstream host's alerts, read with the upstream's reader (Branvil keeps none); no upstream → `ServiceUnsupported`; a refusal, a missing account, or an unreachable host → `ServiceError` (never an empty page) | upstream setting, reader's credential | — | — | each page read from the host at request time; the cursor carries the host's, bound to the upstream | `GET repos/{r}/upstream/dependency-alerts[/{id}]` | contract + provider unit | done |
| `identity(check)` | → `{"branvil_username": ...}`; with `check`, `GET /me` and raise on `401` | member | — | — | — | `GET /me` | provider unit | done: provider unit tests on the double |
| `comment(url, body, status)` | → `CommentWritten` | PR | comment | `comments` | one write txn | `POST pulls/{n}/comments` | contract | done |
| `create_pull_request` | → `PullRequestCreated`; existing open (head, base) returned unchanged; foreign ticket + `closes` → `ServiceUnsupported`; ticket of another repository + `closes` → `ServiceUnsupported`; missing Branvil ticket → `ServiceNotFound` | head branch tip, open PR index, ticket | PR, ticket links | `open_pulls(repo, head, base)` (unique), `ticket_pulls` | lookup + insert in one write txn (single writer ⇒ no double create) | `POST repos/{r}/pulls` | contract + service test | done: smoke |
| `update_pull_request` | body/title; links kept unless `drop_ticket_links` | PR | PR | — | one txn; optional revision | `PATCH pulls/{n}` | contract (indirect) + service test | done: provider unit tests on the double |
| `review` | verdict on `head` → `ReviewSubmitted`; head moved → `ServiceError` mentioning the head, nothing written | head tip | review | `reviews` | tip read and insert compared in the write txn against the recorded tip; Git tip re-read just before commit | `POST pulls/{n}/reviews` | contract | done |
| `review_comment` | as review | head tip | inline comment | `inline` | same | `POST pulls/{n}/inline-comments` | contract | done |
| `reply` | root id of this PR → `ReplyWritten`; other → `ServiceNotFound` | inline root | inline comment | `inline` | — | `POST pulls/{n}/threads/{id}/replies` | contract | done |
| `add_reaction` | comment / inline / review id of this PR | entry | reaction | entry record | read-modify-write in one txn | `POST pulls/{n}/reactions` | contract | done |
| `readiness` | → `Readiness` (rollup, blockers, failed logs as the GitHub provider builds them: codes `base_out_of_date`, `head_changed`, `base_changed`, `checks_failed`, `checks_pending`) | PR, checks, compare, logs | — | `runs(repo, sha)`, `attempts(run)` | PR read twice; head/base change between reads is a blocker | `GET pulls/{n}`, `GET pulls/{n}/checks`, `GET jobs/{id}/log` | contract + provider unit | done: smoke |
| `open_pull_requests(repo, branch)` | → URLs | open PRs of head | — | `open_pulls` | — | `GET repos/{r}/pulls?state=open&head=` | contract | done |
| `artifact(url, name, limit)` | current head's artifact; above limit → `ServiceError`; none → `ServiceNotFound` | head tip, artifacts | — | `artifacts(repo, sha, name)` | — | `GET commits/{sha}/artifacts/{name}` | contract | done |
| `clone_url(repo)` | → `{base}/git/{repo}.git` (no credential; must round-trip through `git remote get-url`) | repo | — | — | — | `GET repos/{r}` | contract | done: smoke |
| `default_branch` | → branch | repo | — | — | — | `GET repos/{r}` | contract | done |
| `pull_request_head` | → `PullRequestHead(head_repository, head)` | PR | — | — | — | `GET pulls/{n}` | contract | done |
| `git_credential()` | → `GitCredential(username=branvil_username, password=token)` (never `None`) | member secret | — | — | — | — | provider unit | done: provider unit tests on the double; smoke |
| `check_push(repo)` | `check_path(workspace, repo)` | — | — | — | — | — | contract | done |
| `commit_url` | → `{base}/{repo}/-/commits/{sha}` | — | — | — | — | — | provider unit | done: provider unit tests on the double |
| `repository_from_remote` | `{base}/git/{repo}.git` (also without `.git`) of the same scheme/host/port → repo; else `None` | — | — | — | — | — | contract | done |
| `pull_request_candidates` | GitHub patrol semantics (feedback / review requested / new head / unanswered; status holds; review limit 3 → draft + `review_limit` status) | patrol entries | draft flag + status comment at the limit | `pulls_by_member(member, pull)` | each PR judged on one activity snapshot | `GET /pulls?involves=me`, `PATCH pulls/{n}` (`draft`), `POST comments` | provider unit + contract | done: provider unit tests on the double |
| `refresh_pull_request` | same test for one PR | PR + activity | as above | — | — | `GET pulls/{n}`, `GET pulls/{n}/activity` | provider unit | done: provider unit tests on the double |

## TicketService

| Operation | Input → output / errors | Reads | Writes | Index | Atomicity / races | API | Test | State |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `read(tickets)` | → `Ticket` (key = number, tracker = repository = the repository path, lane) | ticket | — | `tickets(repo, n)` | — | `GET tickets/{n}` | contract | done: smoke |
| `read(ticket_comments)` | → `Comment` page | comments | — | `comments` | — | `GET tickets/{n}/comments` | contract | done |
| `comment` | status in `meta` | ticket | comment | `comments` | one txn | `POST tickets/{n}/comments` | contract | done |
| `create_ticket(tracker, ...)` | `tracker` is a repository path; the ticket goes to the end of lane `new`; undefined label → `ServiceError("... not defined ...")` | repository labels | ticket, counter | `tickets`, `cards`, `ticket:<repo>` counter | number allocation + insert in one txn | `POST repos/{r}/tickets` | contract | done: smoke |
| `update_ticket` | label add/remove against stored set; `state_changed` | ticket | ticket | `labels(repo,label,ticket)`, `closed(workspace, closed_at)` | one txn: concurrent label changes are merged, not overwritten | `PATCH tickets/{n}` | contract + service concurrency test | done: smoke |
| `add_reaction` | comment of this ticket | comment | reaction | — | one txn | `POST tickets/{n}/reactions` | contract | done |
| `candidates` | GitHub ticket semantics: assigned (since assignment), ready first then working lane, oldest first; status hold; own last word; working lane with a PR is the patrol's (merged → move to done) | cards of the configured workspace's board | lane `done` when merged | `cards(workspace, lane, rank)`; the ticket's one `assignee` is matched on the record | — | `GET workspaces/{w}/board/cards?lane=ready,in_progress&assignee=me` | contract + provider unit | done: smoke |
| `refresh` | one card re-read | ticket + comments + PRs | as above | — | — | `GET tickets/{n}`, `GET tickets/{n}/comments`, `GET tickets/{n}/pulls` | contract | done |
| `move(task, lane)` | → `True` | ticket | lane | `cards` | one txn | `PUT tickets/{n}/board` | contract | done: smoke |
| `closed_since(start, end)` | → `ClosedItem`s: the closed tickets and the closed pull requests of the workspace's repositories | closed indexes, repository records | — | `closed(workspace, closed_at)`, `closed_pulls(repo, closed_at)` | — | `GET workspaces/{w}/board/closed` | contract + provider unit | done |

## Optional features

| Feature | Branvil |
| --- | --- |
| `request_changes` | supported |
| artifacts | supported |
| `dependency_alerts` | supported for a repository with an upstream (the host's alerts); `ServiceUnsupported` without one |
| ticket created off the board | `ServiceUnsupported` (every ticket is a card) |
| failed logs | supported |
| PRs of another service's ticket | `ServiceUnsupported` |
| merge closes a ticket | supported for tickets of the pull request's repository |

## Not in the ports

Repository creation, a repository's labels, board order, the ticket's assignee, thread
resolution, merge, CI retry/cancel, required checks, the upstream, publishing: reached by
the GUI and the `branvil` CLI only. Workspaces, members, tokens, and members' accounts are
not in the ports either; they are Branvil's public admin API (`api.md`, "Members and tokens
(admin API)"), which a local app holding an owner token from the consent screen may use to
set Branvil up. A pull request merged on the upstream host reads as merged here, the host's
checks of a published head are among its checks, and the host's dependency alerts are the
repository's.
