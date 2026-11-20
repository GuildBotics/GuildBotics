# Branvil HTTP API (v1)

This is the internal contract between the service and its clients in this
repository: the browser GUI, the CI runner (`guildbotics runner`), the Git
hooks, and the Hub's update commands (`guildbotics server hub-data backup`,
`hub-verified`, `hub-clients`). GuildBotics' members call the operations
in process through `api::DataApi`, without a token: `DataApi::owner()` acts as
the owner and `DataApi::agent(name, display_name)` as the agent `name`,
created with that display name when missing. When the service changes an
endpoint, this file changes in the same commit.

Some operations below are **in process only**: no client of the server uses
them over HTTP, so the server does not serve them, and members reach them
through `api::MemberApi`. Only the integration tests' server
(`test_support`) serves them, to drive the service as before. They are
marked "(in process only)".

## Browser sessions and the owner's token

GuildBotics authenticates the owner (`host::Host`, given to
`prepare_embedded`). `GET /api/v1/session` resolves GuildBotics' live owner
cookie (`guildbotics_session_<port>`) to the owner member and returns
`{"me", "csrf", "host_navigation": {label, url}}` with GuildBotics' CSRF
value; every non-GET request made with the cookie sends it as
`X-GuildBotics-CSRF`. A Bearer token that is not a member's token
(`bvt_...`) is checked as GuildBotics' owner token (the one in
`GUILDBOTICS_HOME/data/server/<port>/owner.token`; consent tokens of apps are
refused). The browser enters through GuildBotics (`guildbotics open`).

## Conventions

- Base URL: the configured `base_url`, `http://127.0.0.1:7878` by default. Every
  machine uses the same base URL (see `README.md`, "Several machines"), so URLs
  recorded anywhere stay valid everywhere.
- Every API path is under `/api/v1`. JSON in, JSON out, UTF-8.
- Path parameters that are paths (`{repo}`) are one percent-encoded segment:
  `acme/demo` is sent as `acme%2Fdemo`. Branch names in a path are encoded the
  same way.
- Timestamps are RFC 3339 UTC with a `Z` suffix and millisecond precision:
  `2026-10-14T10:41:20.123Z`. They compare correctly as strings.
- Ids are opaque strings. An entry id (comment, review, inline comment, CI
  attempt, artifact) means something only together with the item URL path it
  is sent under; an id of another item is answered with `404 not_found`.
- Collections return `{"items": [...], "next_cursor": "..." | null}` and accept
  `limit` (1-100, default 30) and `cursor`. A cursor is signed by the service
  and bound to the collection and its conditions; any other cursor is
  `400 invalid_cursor`.
- `POST` requests that create something accept an optional
  `Idempotency-Key` header (1-128 printable ASCII); requests whose response
  holds a credential or a code ignore it (token issuance, entry links,
  and sessions), and the read-only
  Markdown resolver, so the service never
  keeps one; an attachment's upload, whose body is an image, ignores it too.
  A repeated key from the same
  member with the same body returns the first response; with another body it is
  `409 idempotency_conflict` (the body is compared together with the request
  path). Requests with the same key from the same member run one after
  another (the second gets the first's response); other keys are not held
  up. Keys are kept for 24 hours.
- Status codes: a create answers `201` (members, tokens, entry links,
  workspaces, repositories, tickets, attachments, comments, reviews, inline comments, replies;
  pull requests as described there); reactions answer `200`; a
  `DELETE` that leaves nothing to show (token) answers `204`; every other
  success is `200`.

### Names and paths

- A name is `[A-Za-z0-9_.-]{1,64}`, not `.`, `..`, or `-` (the web URL separator), not ending in `.git`.
- A workspace is a name (`acme`). A repository path is its workspace's name
  and its own name joined by `/` (`acme/demo`), exactly two names; the
  workspace part is spelled as the workspace is. Names and paths are
  case-preserving and compared case-insensitively (two that differ only in
  case cannot both exist).
- A repository holds its tickets and its pull requests and numbers each from
  1, apart from the other: ticket `#12` and pull request `!12` are two items.
  A number in a path or URL is decimal without leading zeros.

### Web URLs (permanent references)

| What | URL |
| --- | --- |
| Ticket | `{base}/{repo}/-/tickets/{number}` |
| Pull request | `{base}/{repo}/-/pulls/{number}` |
| Ticket comment | `{base}/{repo}/-/tickets/{number}#{comment_id}` |
| PR comment, review, or inline reply | `{base}/{repo}/-/pulls/{number}#{entry_id}` |
| Commit | `{base}/{repo}/-/commits/{sha}` |
| CI attempt | `{base}/{repo}/-/jobs/{attempt_id}` |
| Git remote | `{base}/git/{repo}.git` |

Paths in these URLs are not percent-encoded (each name is URL-safe). The API's
`url` fields and GuildBotics' recorded references use these addresses. The GUI
serves each of them, including reviews addressed by their entry id.

The GUI links each comment's timestamp to its permanent address and offers a
copy button. Opening a fragment waits for the conversation to load, expands a
resolved thread when needed, and scrolls to the highlighted entry. URLs copied
in a ticket drawer use the ticket's page, not the board's address.

### Markdown references (GUI)

`POST /repos/{repo}/markdown` takes `{"source": "See #3 and !2"}` (up to
256 KiB, empty allowed) and returns `{"markdown": "..."}`. The owner and
agents may use it; runners may not. It reads current state, creates nothing,
ignores `Idempotency-Key`, and uses the usual session CSRF check. Existing
ticket, pull request, comment, and review bodies remain the original Markdown.

The result is that Markdown with prose references to existing Branvil items
replaced by inline HTML anchors. `#N` and `!N` use `{repo}`;
`workspace/name#N`, `workspace/name!N`, and bare Branvil web URLs may name
another repository. Anchors carry their canonical web URL, the target title
(HTML-escaped), `data-branvil-kind="ticket|pull"`, and
`data-branvil-state="open|closed|merged"` (`merged` is only for PRs).
Clients render Markdown, sanitize the HTML, then use these attributes for
icons and accessible labels. This response is **not trusted HTML**.

Code spans/blocks, links (including explicit autolinks), images, and HTML
(inline contents included) are untouched. Escaped sigils and non-whole
references are literal. An unclosed inline HTML tag excludes text only
within its Markdown block, so later paragraphs, list items, headings,
and table cells are independent.
Unknown numbers and foreign repositories stay text; the first character
of an unresolved bare Branvil URL is an HTML entity to
prevent GFM from auto-linking it. The parser and lookup are shared with the
upstream rewrite. The GUI refreshes displayed Markdown on ticket, pull,
repository, and upstream events, including changes in another repository.

### Authentication

- Members are the owner (the one person who uses the service, created at the
  server's first start), agents, and CI runners.
- API clients send `Authorization: Bearer <token>`: a member's token, shown
  once when it is issued and stored only as a SHA-256 hash, or GuildBotics'
  owner token (see above).
- Git over Smart HTTP uses HTTP Basic: the username is the member's name and
  the password is one of the member's tokens. Both fetch (`git-upload-pack`) and
  push (`git-receive-pack`) require it. A username that is not the token's
  member (compared case-insensitively) is refused with `401` and a
  `WWW-Authenticate: Basic` challenge.
- Pushes are checked by the service's own pre-receive hook: the owner or an agent
  (a runner pushes nothing); only `refs/heads/*` and `refs/tags/*`; a
  protected branch (the default branch always) is never pushed, deleted, or
  force-pushed: it moves only by a merge. The one exception is creating a
  protected branch that does not exist yet (the first import into an empty
  repository) by the owner. An agent creates tags; only the owner moves or
  deletes one.
- A cookie request without the right `X-GuildBotics-CSRF` on a non-GET method
  is `403 csrf_mismatch`, before the handler runs. The GUI reads
  `GET /api/v1/session` to refresh its CSRF value and retries that request
  once with the same body and idempotency key. If the session cannot be
  refreshed, it asks the user to reload the page.
- GuildBotics checks `Host` and `Origin` on every request before the service
  sees it (`421 misdirected` / `403`).

### Errors

```json
{"error": {"code": "not_found", "message": "Ticket 9 was not found in acme/demo."}}
```

| HTTP | `code` | Meaning |
| --- | --- | --- |
| 400 | `invalid` | The request is malformed or a value is out of range. |
| 400 | `invalid_cursor` | The cursor is not one this collection gave. |
| 400 | `label_undefined` | A label the ticket's repository does not define (message lists the defined ones). |
| 401 | `unauthorized` | No valid token or session. |
| 403 | `forbidden` | The member's kind may not do this (see "Members"). |
| 403 | `csrf_mismatch` | A cookie-authenticated write has a missing or mismatched CSRF header. Nothing was written; read the current session and retry once. |
| 404 | `not_found` | The item, entry, or path is not there. |
| 409 | `conflict` | A revision or expected value did not match. |
| 409 | `head_moved` | The pull request head is no longer the `commit` sent. Nothing was written. |
| 409 | `base_moved` | The base moved during a merge. Nothing was written. |
| 409 | `not_mergeable` | Merge preconditions are not met (message says which). |
| 409 | `children_open` | A ticket is closed as completed while one of its children is open. |
| 409 | `blocked` | A ticket is moved into `in_progress` while one of its blockers is open. |
| 409 | `delete_has_children`, `delete_blocks_tickets`, `delete_linked_pull`, `delete_upstream_issue` | Ticket deletion is refused because it has children, is named as a blocker, links a PR, or links an upstream issue, respectively. Closed children, dependents, and PRs count too. |
| 409 | `no_account` | The member whose account the upstream host needs has none there, or its credential is not on this machine ("Upstream"). |
| 409 | `upstream_refused` | The upstream host refused what Branvil sent or asked for (the message says why). |
| 409 | `idempotency_conflict` | See conventions. |
| 413 | `too_large` | Body, log, artifact, or image above its limit. |
| 421 | `misdirected` | Wrong `Host`. |
| 500 | `internal` | An unexpected failure; the detail is in the service log only. |
| 422 | `unsupported` | The operation is not offered. |
| 422 | `closes_foreign` | A closing ticket link names another repository or another service. Use a reference link instead. Nothing was written. |
| 503 | `unavailable` | Temporarily unable (storage busy, CI queue full). Safe to retry reads. |

Messages never contain tokens.

## Service and members

### `GET /api/v1/me`

```json
{"name": "aiko", "display_name": "Aiko", "kind": "agent",
 "accounts": [{"provider": "github", "login": "aiko-bot", "kind": "token", "set_at": "..."}],
 "reviewers": ["ken"]}
```

`kind` is `owner`, `agent`, or `runner` (the CI runner's member). `accounts`
are its accounts on upstream hosts (see "Upstream"); an account's `kind` is
`token` or `app`. `reviewers` are the members asked to review every pull
request it opens (see "Pull requests"). `401 unauthorized` for a
bad token.

### Members and tokens (admin API)

The members, tokens, accounts ("Upstream"), and workspaces endpoints are
Branvil's admin API: any client holding an owner token may use them (the
`branvil` CLI, the GUI). They are as stable
as the rest of this file.

- `GET /api/v1/members` (owner) → items of `{"name","display_name","kind","accounts","reviewers","avatar_url"}`;
  `avatar_url` is where the member's picture is (`null` for none), and it
  changes with the picture.
- `GET /api/v1/members/{name}` (in process only; owner, or the member itself) → member; `403
  forbidden` for another member's name (known or not) asked by a non-owner,
  `404 not_found` for an unknown name asked by the owner.
- `POST /api/v1/members` (owner) `{"name","display_name"?,"kind": "agent"|"runner"}` → member.
  The name `branvil` is reserved for service comments and cannot be a member.
- `PUT /api/v1/members/{name}/avatar` (owner, or the member itself) with the
  image as the body → member: sets the member's picture, a PNG, JPEG, GIF, or
  WebP image (read from its bytes, not from `Content-Type`; `400 invalid`
  otherwise) of at most 4 MiB (`413 too_large`). The same picture again keeps
  `avatar_url`. `DELETE` (same callers) removes it → member.
- `GET /api/v1/members/{name}/avatar` (any member) → the image with its media
  type; `404 not_found` for none. It may be cached for good, as its
  `avatar_url` changes with it.
- `PATCH /api/v1/members/{name}` (owner) `{"display_name"}`
- `PUT /api/v1/members/{name}/reviewers/{reviewer}` (owner) and `DELETE` → member:
  ask or stop asking `reviewer` to review every pull request the member opens.
  A reviewer is an owner or an agent other than the member, kept once and
  spelled as the member is named: asking a runner or the member itself is
  `400 invalid`, and a runner member has no reviewers (`400 invalid` for both
  methods). An unknown name is `404 not_found`. Asking again or withdrawing
  one that is not asked changes nothing.
- Token endpoints are the owner's for other members; a member may issue, list, and revoke its own tokens.
- `POST /api/v1/members/{name}/tokens` `{"label", "replace"?: true}` →
  `{"id","label","token","created_at"}` (the only time `token` is shown). With
  `replace: true` the member's tokens of that label are revoked in the same
  write as the issue, so a retried call leaves exactly one. The labels Branvil
  issues itself are refused with `400 invalid`: `owner-file` (the owner's
  token file) and `auto` (the automatic runner).
- `GET /api/v1/members/{name}/tokens` → items of `{"id","label","created_at","last_used_at"}`
- `DELETE /api/v1/members/{name}/tokens/{id}` revokes. To revoke by label, list
  and delete each token of it.

What a member may do follows from its kind alone, the same in every
workspace. The owner may do everything. An agent reads everything and makes
the writes of the endpoints below that carry no mark: tickets, comments,
reactions, reviews, pull requests, CI retries and cancels, and pushes of
unprotected branches. Endpoints marked "(owner)" (creating workspaces and
repositories, repository settings and upstreams, members and their accounts,
backups, diagnostics, entry links), merging, publishing, and marking a pull
request ready for review are the owner's (`403 forbidden` for an agent). A runner uses only the runner API (`403 forbidden` elsewhere,
except `/me`, its own member record, and its own tokens).

## Workspaces

A workspace holds repositories (`<workspace>/<name>`) and has one board.

- `GET /api/v1/workspaces` → items of `{"name": "acme", "lanes": ["new","ready","in_progress","done"]}`, by name.
- `GET /api/v1/workspaces/{workspace}` → workspace.
- `POST /api/v1/workspaces` (owner) `{"name"}` → `201` workspace; `409 conflict` when it exists.

## Repositories

### `GET /api/v1/repos/{repo}`

```json
{
  "path": "acme/demo",
  "default_branch": "main",
  "clone_url": "http://127.0.0.1:7878/git/acme/demo.git",
  "url": "http://127.0.0.1:7878/acme/demo",
  "protected_branches": ["main"],
  "required_checks": ["test"],
  "post_merge_jobs": [],
  "required_approvals": 1,
  "auto_land": false,
  "hold_while_failing": false,
  "ci_network": false,
  "responder": {"member": "aiko", "chosen": false, "automatic": "aiko"},
  "labels": ["bug", "feature"],
  "upstream": null,
  "revision": 4
}
```

`labels` are the labels its tickets may carry. `required_approvals` is how
many approvals a merge or a publish needs (`0` needs none; a new repository
needs 1; see "Pull requests"). `auto_land` says the repository lands its pull
requests by itself (`false` for a new repository; see "Landing
automatically"). `upstream` is the repository on
a code host it follows and publishes to, or `null` (see "Upstream").

- `GET /api/v1/repos` → items (every repository), by path.
- `POST /api/v1/repos` (owner) `{"path","default_branch"?,"labels"?}` (default `main`, no labels) → repository. `path` is `<workspace>/<name>` of an existing workspace (`404 not_found` otherwise). An empty bare repository is created.
- `PATCH /api/v1/repos/{repo}` (owner) `{"revision", "default_branch"?, "required_checks"?, "post_merge_jobs"?, "required_approvals"?, "auto_land"?, "hold_while_failing"?, "protected_branches"?, "ci_network"?, "responder"?, "labels"?}` → `409 conflict` when `revision` is stale. Removing a label leaves it on the tickets that carry it. `ci_network` is the network access of CI jobs (`docs/ci.md`; the CI tab of the repository's settings). With an upstream, the default branch follows the upstream's and cannot be changed (`409 conflict`).

### `GET /api/v1/repos/{repo}/branches/{branch}`

`{"name": "feature", "sha": "<40 hex>"}`; `404 not_found` when there is no
branch of exactly that name.

### Dependency alerts (in process only)

The dependency vulnerabilities the upstream host reports for the repository
it follows ("Upstream"; GitHub: Dependabot alerts). Branvil keeps none of them
and judges nothing: each request reads the host with the upstream's reader,
and only its account (another member's is never tried). Any member who sees
the repository may read them.

- `GET /api/v1/repos/{repo}/upstream/dependency-alerts?state={state}` →
  items in the host's order. `state` is `open` (the default), `resolved`, or
  `dismissed`; `limit` and `cursor` as for any collection. The cursor carries
  the host's own and is bound to the repository, `state`, and the upstream
  (provider and repository) it was read from: after the upstream changes it
  is `400 invalid_cursor`. An empty `items` is the host's answer that it has
  no such alert.
- `GET /api/v1/repos/{repo}/upstream/dependency-alerts/{id}` → one alert.

```json
{"id": "12", "state": "open",
 "url": "https://github.com/acme/demo/security/dependabot/12",
 "package": "lodash", "ecosystem": "npm", "manifest_path": "package-lock.json",
 "severity": "high",
 "identifiers": [{"type": "GHSA", "value": "GHSA-35jh-r3h4-6jhm"},
                 {"type": "CVE", "value": "CVE-2021-23337"}],
 "summary": "Command Injection in lodash", "description": "...",
 "affected_versions": "< 4.17.21", "patched_version": "4.17.21",
 "created_at": "2026-10-20T08:00:00.000Z", "updated_at": "2026-10-21T09:30:00.000Z"}
```

`id` is the host's number of the alert, a decimal string. Every field but
`id`, `state`, and `identifiers` may be `null`; `patched_version` is `null`
when no version fixes it. `state` is the host's state in these three words
(GitHub: `fixed` is `resolved`, `auto_dismissed` is `dismissed`).

A failure is never an empty list:

| HTTP | `code` | When |
| --- | --- | --- |
| 422 | `unsupported` | The repository has no upstream. |
| 409 | `no_account` | The reader has no account on the host, or its credential is not on this machine. |
| 404 | `not_found` | No alert of that `id`, or the host answered "not found" for the repository: the reader's account cannot see it, or the host keeps no alerts of it. |
| 409 | `upstream_refused` | The host refused the reader's request (its token, its permissions, or the request itself). |
| 503 | `unavailable` | The host could not be reached, failed, or its rate limit for the account is used up. |

A host's answer seldom says which of its causes it is, so the message says
what the host said and what to check rather than guessing. On GitHub the
reader's account needs, for a GitHub App, Repository permissions →
Dependabot alerts: Read-only and access to the repository, with the added
permission approved on the installation (GitHub asks the account that
installed the app to approve it); for a fine-grained token the same
permission; for a classic token the `security_events` (or `repo`) scope.
Dependabot alerts must be enabled for the repository.

Repository responses include `responder: {"member": "aiko", "chosen": false, "automatic": "aiko"}`.
`member` is the effective assignee. `automatic` always names the current automatic
candidate, including when `chosen` is true; the CI editor uses it to preview
returning to automatic selection. Automatic selection is evaluated once for each response.
The owner may PATCH `responder` with an existing agent name (stored with the
member's canonical spelling), or `null` to return to automatic selection.
Omission keeps the setting. Owner, runner, and unknown names return 400
`invalid` without changing the record or revision; non-owners receive 403.
New repositories use automatic selection. Repository responses preserve the configured
responder, even while that agent is inactive; unrelated settings changes must not clear
it. The automatic preview uses the stored agent authors of merged PRs, then agent names
in person_id order. Actual repair creation restricts both authors and responders to the
host's fresh active roster. If none qualifies, it chooses the first active agent by
person_id. With no active agent, creation stays pending while the verification API and
CI screen continue to report `failing`, its commit and failed jobs. A later roster refresh
retries creation without assigning a human.


## Tickets and the board

A ticket belongs to a repository. The repository's workspace has one board;
every ticket is on it, in a lane.

### Ticket shape

```json
{
  "url": "http://127.0.0.1:7878/acme/demo/-/tickets/3",
  "repository": "acme/demo",
  "key": "3",
  "title": "Fix login",
  "repair": false,
  "body": "It fails.",
  "mentions": [],
  "state": "open",
  "state_reason": null,
  "author": "aiko",
  "assignee": {"name": "aiko", "assigned_at": "2026-10-14T10:00:00.000Z"},
  "labels": ["bug"],
  "interactive": false,
  "interactive_ancestor": null,
  "upstream_issue": "https://github.com/acme/demo/issues/12",
  "lane": "ready",
  "rank": "0|hzzzzz",
  "parent": {"url": "http://127.0.0.1:7878/acme/demo/-/tickets/1", "repository": "acme/demo",
             "key": "1", "title": "Sign-in", "state": "open", "state_reason": null},
  "children": [],
  "blocked_by": [ ...the same shape... ],
  "blocks": [],
  "created_at": "...",
  "updated_at": "...",
  "closed_at": null,
  "revision": 7
}
```

`repair` is true for a system-created repair ticket, on both tickets and board
cards. Its author is the reserved name `branvil` (displayed with the same anvil
avatar as system comments). Ordinary tickets have `repair: false`. Repair tickets
are assigned in `ready` at its front; passing default-branch verification closes
them as completed and moves them to `done`. A linked PR merge alone does not
complete a repair ticket.


`key` is the ticket's number as a string (it reads `#3`). `state` is `open` or
`closed`; `state_reason` is `completed`, `not_planned`, or `null`. A ticket has at most one assignee, the agent that works on it
(`assignee` is `null` when nobody is assigned). `lane` is the lane of its workspace's board the
ticket is in: `new`, `ready`, `in_progress`, or `done` (every ticket is a card
of that board), and `rank` orders it within the lane. `upstream_issue` is the issue the
ticket stands for on its repository's upstream host, or `null`; publishing a
pull request that links the ticket writes a line about it there.
`interactive` says that a person works on the ticket interactively, with an
agent at hand; it is `false` for a new ticket and changes only by `PATCH`.
Branvil keeps and shows it and acts on it nowhere; a client that works on
tickets by itself may leave such tickets to the person.
`interactive_ancestor` is the nearest ancestor (its parent, the parent's
parent, …) whose `interactive` is `true`, in the shape of `parent`, or `null`: the
ticket is a part of that interactive work too, whatever its own
`interactive`.

`mentions` lists existing Branvil member names mentioned in the body, without
duplicates, in occurrence order and with their stored spelling. Matching is
ASCII case-insensitive. A mention is `@name`: before `@` there may not be an
ASCII letter, digit, `_`, `.`, `@`, or `-`; after the name there may not be an
ASCII letter, digit, `_`, `-`, or a dot followed by one of those. Thus `@bot.`
mentions `bot`, while `mail@bot`, `@bot-extra`, and `@bot.example` do not.
This is literal matching, including inside code and quotations. Names are
resolved when the body is written; unknown names are not kept as mentions.

Tickets of one workspace relate in two ways, each shown from both sides with
the other ticket's title and state:

- **Parent and children.** A ticket has at most one `parent` and any number of
  `children`. A parent is not closed as `completed` while one of its children
  is open (a child closed as `not_planned` is closed): `PATCH` to `closed` /
  `completed` (also from `not_planned`) and the owner's move into `done` are
  refused with `409 children_open`. A merge whose pull request closes such a
  parent leaves it as it is; a merge that closes the children too closes the
  parent after them, whatever the order of the links. Closing as
  `not_planned` and an agent's move into `done` are not refused.
- **Blockers.** A ticket is `blocked_by` any number of tickets (and `blocks`
  the tickets that name it). It is not started while one of them is open: a
  move into `in_progress` from another lane is refused
  with `409 blocked` (moving within `in_progress` is not).

The rules hold at those moves only: reopening a child or a blocker, or adding
a relation to a closed or started ticket, is not refused and is shown as it
is. A relation that would make tickets wait on each other forever (a ticket
its own ancestor or blocker, a child blocked by its ancestor, a cycle of
blockers, …) is refused with `400 invalid`, as is one to a ticket of another
workspace.

- `POST /api/v1/repos/{repo}/tickets` `{"title","body","labels","lane"?,"interactive"?,"assignee"?,"parent"?,"blocked_by"?,"upstream_issue"?}` → `201` ticket with the repository's next number, at the end of `lane` of the workspace's board (when `lane` is omitted: `ready` with a parent or blockers, `new` otherwise) in the same transaction. Optional `interactive` (default false), `assignee` (member name, including the owner), `parent` (ticket URL), and `blocked_by` (ticket URLs) are committed with the new ticket and its indexes before any event is published. Relations must name tickets of the same workspace and must not create a wait cycle. `in_progress` is refused while a blocker is open (`409 blocked`), as when moving an existing ticket. Invalid assignment or relations roll back the entire creation, including its number. Labels must be defined by the repository (`400 label_undefined`).
- `GET /api/v1/repos/{repo}/tickets?state=open|closed|all&lane=...&assignee=...&label=...&q=...` (in process only) → items in number order
  (`state` defaults to `open`; `assignee=me` is the requesting member).
  `q` splits on whitespace and requires every word to occur in the title or
  body, ignoring case (including Unicode). An omitted, empty, or whitespace-only
  `q` does not filter. It combines with all other conditions and pagination;
  cursors belong to the original query, including `q`.
- `GET /api/v1/repos/{repo}/tickets/{number}` → ticket.
- `DELETE /api/v1/repos/{repo}/tickets/{number}` (owner only; agents and runners receive `403 forbidden`) → `204`. Permanently removes the ticket, all its comments and reactions, its own parent/blocker links, and their derived indexes in one transaction. Refused with `409` and a specific reason (above) if it has any child, is named as another ticket's blocker, is linked from any PR, or has an `upstream_issue`; a refusal changes nothing. Missing tickets return `404`. Numbers are never reused. Attachment references are removed with each body/comment; repository attachments still referenced elsewhere remain, and unreferenced ones follow the usual attachment sweep. Emits `ticket` events for the deleted ticket and its former parent/blockers, plus a `board` event for the workspace. The GUI offers Delete last in the owner’s card menus on the task board and task flow, asks for irreversible-deletion confirmation, and shows a refusal's reason.
- `PATCH /api/v1/repos/{repo}/tickets/{number}` — every field optional:
  `{"title", "body", "add_labels": [], "remove_labels": [], "interactive", "state", "state_reason", "upstream_issue", "parent", "add_blocked_by": [], "remove_blocked_by": [], "revision"}`.
  `parent` is the URL of a ticket of the same workspace, or `null` for none;
  `add_blocked_by` / `remove_blocked_by` are such URLs, changed against the
  stored set as labels are. A child names its parent and a blocked ticket its
  blockers, so a ticket's `children` and `blocks` change by patching those
  tickets.
  `upstream_issue` is an issue URL of the repository's upstream host (`400
  invalid` otherwise, and for a repository without an upstream), or `null` to
  clear it.
  Labels are added and removed against the stored set inside one transaction,
  so concurrent label changes of others are kept; removing a label the ticket
  does not carry changes nothing. `revision` is optional and, when given, makes
  the whole update conditional (`409 conflict`). Closing without a
  `state_reason` records `completed`; reopening clears `state_reason` and
  `closed_at`. Closing a ticket that is in another lane of the board moves it
  to `done` (reopening leaves it there). The response is
  `{"ticket": {...}, "state_changed": true}`.
- `GET /api/v1/repos/{repo}/tickets/{number}/pulls` → items of
  `{"url", "label", "title", "state", "merged", "closes", "updated_at"}`: the pull
  requests of this Branvil that link the ticket, oldest first.
- `PUT /api/v1/repos/{repo}/tickets/{number}/assignee` `{"name"}` → ticket: sets the
  assignee. Setting the current assignee again keeps its `assigned_at`; another name replaces it
  with a new `assigned_at`; `404` for an unknown member.
- `DELETE /api/v1/repos/{repo}/tickets/{number}/assignee` → ticket with `"assignee": null`
  (also when nobody was assigned).
- `PUT /api/v1/repos/{repo}/tickets/{number}/board` `{"lane", "before"?: "<repo>#<number>", "after"?: "<repo>#<number>"}`
  puts the ticket in a lane of its workspace's board (moving it if it is in
  another lane). `before`/`after` place it next to another card of that lane,
  named by its repository path and number (`acme/demo#2`; `404` when that is
  not a card of that lane; both together must name adjacent cards); without
  them it goes last. The response is the ticket. `lane` is required: a ticket
  does not leave the board (`{"lane": null}` is `400 invalid`). The owner's move also sets
  the state: into `done` closes an open ticket as `completed`, into another
  lane reopens a closed one. An agent's move leaves the state alone (closing a
  ticket is a person's decision). A move into `in_progress` of a blocked
  ticket (`409 blocked`) and the owner's move into `done` of a parent with an
  open child (`409 children_open`) are refused, as under "Ticket shape".
- `PUT /api/v1/repos/{repo}/tickets/{number}/repository` `{"repository", "revision"?}`
  moves the ticket to another repository of the same workspace, with that
  repository's next number, in one transaction. The response is
  `{"ticket": {...}, "removed_labels": []}`: the moved ticket, and the labels it
  carried that the new repository does not define, which it no longer carries.
  Everything else stays, including its lane and rank on the board, its
  assignee and its relations. Its comments with their ids and reactions move
  with it; the tickets that name it as their parent or blocker and the pull
  requests that link it name its new URL, without changing their revisions.
  Its images stay in the repository they were uploaded to (see "Attachments").
  A pull request that has not merged and is not of its new repository no
  longer closes it (a merge closes only the tickets of its own repository,
  also after a reopen); a merged one keeps what it closed.
  Its old number is not reused and keeps leading to it: every operation on
  `/api/v1/repos/{old repo}/tickets/{old number}` and its old URL (as a
  relation or a pull request's link) acts on the moved ticket and answers with
  its new `url`, through each move it made. References to its old number or
  URL in Markdown link to it where it is now, and a published pull request
  names it there. A relative reference in its own text (`#7`) is read in its
  new repository. `400 invalid` for its own
  repository or one of another workspace, `404` for an unknown repository,
  `409 conflict` for a stale `revision`, `409 move_upstream_issue` while it
  has an `upstream_issue` (an issue of its repository's upstream host),
  `409 move_repair` for a repair ticket (it repairs its repository's failing
  checks), and
  `409 move_closing_pull` while an open pull request closes it (a merge closes
  only the tickets of its pull request's repository); a refusal changes
  nothing. Emits `ticket` events for its old and new place and the tickets
  related to it, `pull` events for the pull requests that link it, and a
  `board` event. The GUI offers it in the repository field of the ticket's
  page.

### Comments (tickets and pull requests alike)

The comment shape:

```json
{"id": "cmt_01JA...", "author": "aiko", "body": "Looking into it.",
 "mentions": [],
 "meta": {"kind": "workflow_error", "routing": "suppress", "reason": "failed"},
 "created_at": "...", "reactions": [{"author": "ken", "reaction": "ack"}],
 "url": "http://127.0.0.1:7878/acme/demo/-/tickets/3#cmt_01JA..."}
```

`meta` is an opaque JSON object (at most 4 KiB serialized) a client stores with
the comment and gets back; it is `null` when none was sent. The GUI shows a
comment with `meta.routing == "suppress"` as a status notice.
`mentions` follows the same rules as ticket bodies and is recalculated on
comment edits. These fields describe conversation comments, not inline threads.

- `GET .../tickets/{number}/comments` → items, oldest first.
- `POST .../tickets/{number}/comments` `{"body", "meta"?}` → `201` comment.
- `PATCH .../tickets/{number}/comments/{id}` `{"body"}` → the updated comment.
  Only its author or the owner may edit it (`403 forbidden` otherwise).
  The body must be nonempty, as when posting. Metadata, author, creation time,
  position, id, URL, and reactions stay unchanged; no edit history or edit time
  is stored.
- `DELETE .../tickets/{number}/comments/{id}` → `204`. Only its author or the
  owner may delete it. The comment, its reactions, and its id index are removed
  permanently; subsequent requests for that id return `404 not_found`.
  Both operations require a comment of this ticket (`404` for another ticket,
  a pull request, or a missing id), update the ticket's `updated_at`, and emit
  a `ticket` change event. These operations apply to ticket comments only.
- `POST .../tickets/{number}/reactions` `{"target_id", "reaction"}` where
  `reaction` is `ack`, `agree`, `celebrate`, or `support` → `{"id","reaction"}`.
  The target must be a comment of this ticket. `DELETE` with the same body
  takes the caller's own reaction back → `204` (also when there was none).

### Attachments (images of tickets)

An image belongs to a repository, not to one ticket, so it can be uploaded
before the ticket it goes in exists. A ticket's body or a ticket comment
refers to it by writing its `url`: `![screen.png](/api/v1/repos/acme%2Fdemo/attachments/atc_01JA...)`.

```json
{"id": "atc_01JA...", "name": "screen.png", "media_type": "image/png", "size": 48213,
 "url": "/api/v1/repos/acme%2Fdemo/attachments/atc_01JA...",
 "created_at": "..."}
```

`url` is a same-origin path (the GUI shows it as an `<img>` with its session);
a client with a token gets it under the base URL.

- `POST /api/v1/repos/{repo}/attachments?name={name}` with the image as the
  body → `201` attachment. It must be a PNG, JPEG, GIF, or WebP image, read
  from its bytes, not from `Content-Type` (SVG is refused: `400 invalid`), of
  at most 10 MiB (`413 too_large`). `name` is at most 255 bytes without
  control characters (`400 invalid`); without one it is `image`. An
  `Idempotency-Key` is ignored: an upload repeated leaves an attachment no
  ticket refers to.
- `GET /api/v1/repos/{repo}/attachments/{id}` → the image with its media type
  as read at the upload. It may be cached for good: an attachment never
  changes. An id of another repository is `404 not_found`.
- `GET /api/v1/repos/{repo}/tickets/{number}/attachments` (in process only) → items of the
  attachment shape plus `"referenced_by": ["body", "cmt_01JA..."]`: the
  attachments of the workspace's repositories that the ticket's body and
  comments refer to, in the order of their first reference, each with where it
  is referred to (`body`, or a comment's id), in that order. A reference is the
  attachment's id anywhere in the text (inside code too); an id of no
  attachment of those repositories is not listed. Each `url` names the
  repository the image was uploaded to.

Any member who sees the repository uploads and reads its attachments; a runner
gets `403 forbidden`. The tickets of every repository of the workspace keep an
attachment, so a ticket moved to another repository keeps its images where
they are. An attachment that no such ticket body or ticket comment
refers to is deleted once 24 hours have passed since its upload (checked at
start and every hour): one referred to stays while a reference does, and its
last reference going makes it one no ticket refers to. Pull request bodies
and comments do not keep an attachment.

### The board of a workspace

- `GET /api/v1/workspaces/{workspace}/tickets?mentions=me&state=open&comments=all` (in process only) →
  the card shape below for tickets whose body or any ticket comment mentions
  the requesting member. `mentions=me` is required; other values are refused.
  No lane or assignee filter applies, so unassigned tickets in `new` are also
  returned. Branvil keeps every ticket on its repository's workspace board;
  discovery does not depend on the board's currently displayed lanes.
  Only repositories of the named workspace are included. `state` is `open`
  by default, or `closed` / `all`; `comments` is `all` by default, or `none`.
  Multiple mentions return one ticket, including across page boundaries.
  Items are ordered by repository id, then ticket number, and use the usual
  signed pagination. PR conversation comments are not ticket mentions.

- `GET /api/v1/workspaces/{workspace}/board/cards?lane=ready,in_progress&assignee=me&state=open&comments=all` →
  items of *card*: the ticket shape plus

  ```json
  {"comments": [ ...comment shape, all of them, oldest first... ],
   "pull_requests": [{"url", "label", "title", "state", "merged", "updated_at"}]}
  ```

  Every condition is optional: no `lane` is every lane, no `state` is `open`, `comments=none` leaves
  `comments` empty (the GUI uses it; the default is `all`). `assignee=me` means the requesting member. Cards are ordered by lane (as in
  `lanes`) then rank. Pagination follows the conventions; a client that needs
  every card follows `next_cursor` to the end.
- `GET /api/v1/workspaces/{workspace}/board/closed?since={ts}&until={ts}` (in process only) → items of
  `{"kind": "ticket"|"pull_request", "url", "title", "label", "closed_at", "merged"}`:
  the tickets of the workspace's repositories closed in the window, in any
  lane (`label` `#3`), and the pull requests of those repositories
  closed or merged in it (`closed_at` is the merge time when merged). Both
  ends inclusive.
- `GET /api/v1/events` — Server-Sent Events for the GUI (see "Events").

## Pull requests

### Pull request shape

```json
{
  "url": "http://127.0.0.1:7878/acme/demo/-/pulls/3",
  "repository": "acme/demo",
  "number": 3,
  "label": "!3",
  "title": "Fix login",
  "body": "…",
  "state": "open",
  "merged": false,
  "draft": false,
  "author": "aiko",
  "head": "ticket/3",
  "head_repository": "acme/demo",
  "head_sha": "<40 hex>",
  "base": "main",
  "base_sha": "<40 hex>",
  "reviewers": ["ken"],
  "review_states": [
    {"member": "ken", "requested": true, "verdict": "approve", "outdated": true},
    {"member": "mia", "requested": false, "verdict": "approve", "outdated": false}
  ],
  "approvals": 1,
  "unanswered": [{"kind": "review", "id": "rev_...", "author": "ken", "url": "...#rev_..."}],
  "tickets": [{"url": "...", "closes": true}],
  "labels": [],
  "changed_files": 2,
  "ready_at": null,
  "created_at": "...", "updated_at": "...", "closed_at": null, "merged_at": null,
  "merge_sha": null,
  "upstream": null,
  "landing": null,
  "revision": 5
}
```

`state` is `open` or `closed` (a merged pull request is `closed` with
`merged: true`). `head_sha`/`base_sha` are the branches' current tips read from
Git at request time (`""` when the branch is gone); a merged pull request's
`head_sha` is the original reviewed head, retained separately from
`merge_sha`. `merge_sha` is the commit placed on the base: the merge result for a local
merge, or the upstream host's merge commit. The head's approvals and diff stay
attached to the original head even when the merge result has a different SHA.
`reviewers` are the pending review requests: a member leaves it by submitting a review and is back on a new request.
`review_states` is where every member who was asked or reviewed stands (the
reviewer badges of the GUI draw it): first the members who reviewed, in the
order of their first review, then those asked who have not reviewed:
`requested` is an open request, `verdict` the verdict of their last
`approve` or `request_changes` (a `comment` says neither and keeps it), or
`comment` while they only commented (`null` before any review), and `outdated`
whether that review is on a commit other than `head_sha`. The author is never in it. `approvals` counts the states not
`requested` whose verdict is `approve` and not `outdated`; a merge and a
publish need the repository's `required_approvals` of them. `unanswered` lists
the statements that wait on the author's answer, which keep the pull request
from landing: an unresolved thread whose last comment is someone
else's, a review with a body (an approval too), or a conversation comment
without `meta` (a comment with `meta` is a machine's notice; Branvil's own notes
of a conflict or failed host check have none, so they count). `kind` is `thread`,
`review`, or `comment`; `id` and `url` name the entry the author answers: the
thread's last comment, the review, or the comment. The author answers a thread
by replying in it or reacting to its last comment, and a review or comment by
reacting to it or by writing anything after it (a comment without `meta`, a
review, or an inline comment). It is always empty for a pull request whose
author is the owner or which links a ticket of this Branvil that is
`interactive` or has an `interactive_ancestor`, and for a closed one. GuildBotics' patrol gives the
author work on the same statements, so the two agree on when it answered.
`ready_at` is the last time
the pull request was marked ready for review. `upstream` is set once the pull
request was published (see "Upstream"). `landing` is its current queue entry
or last departure, or `null` before it first joins (see "Landing").

- `POST /api/v1/repos/{repo}/pulls` (in process only)
  `{"head", "base"?: "", "title", "body", "draft": false, "tickets": [{"url","closes"}]}`
  → `201 {"created": true, "pull": {...}}`, or `200 {"created": false, "pull": {...}}`
  when an open pull request of the same head and base exists (it is unchanged).
  An empty `base` is the default branch. The new pull request asks the author's
  `reviewers` (see "Members and tokens") to review it; changing them later leaves
  pull requests already open as they are. The head branch must exist (`404 not_found`). A ticket of
  this Branvil must exist (`404 not_found`); `closes: true` is accepted only for a
  ticket of this repository (`422 closes_foreign` otherwise): a ticket of another
  repository, like a URL of another service, is kept as a reference and cannot
  close.
  Concurrent creates of the same head and base produce one pull request.
- `GET /api/v1/repos/{repo}/pulls?state=open&head={branch}&base={branch}` → items.
  Ordered by `updated_at` descending, then PR number descending for equal
  timestamps. `state` is `open` (default), `closed`, or `all`; `head` and `base`
  are optional exact branch filters. Follow `next_cursor` for older results.
  Cursors locate an update and number, not a snapshot: a PR updated between
  requests may move ahead of the cursor; refresh from the first page to see it.
  Add `sort=readiness` for ascending `merge_order: [stage, unanswered, ci]`.
  Stages: 0 merge/following; 1 publishing/host checks; 2 conditions with only
  unanswered/checking left; 3 approved with head checks running; 4 approvals
  missing; 5 conflict, failed checks, already integrated, publication requirements
  or owner repair; 6 draft; 7 closed/merged. `checking` does not affect stage.
  Author replies break ties (0 before 1). CI breaks remaining ties as before:
  passed/no checks, pending, unregistered, failing. Updated time and number
  descend for equal keys. Ranking happens before pagination; cursors bind to
  the chosen sort and filters.
- `GET /api/v1/pulls?involves=me&state=open&draft=false` (in process only) → items of
  *patrol entry*: the pull request shape plus `"activity"` (below), for every
  open pull request the member authored, is a requested reviewer of, has
  reviewed, or wrote an inline comment or thread reply on. The owner reviews and
  merges every pull request, so for the owner it is every open pull request of
  every repository. Ordered so that what waits for least once the member's work is
  done comes first: the member is the author or, otherwise, a reviewer; the head
  is approved (its approvals reach `required_approvals`) or not; its required
  checks failed (one completed without succeeding, or its queue entry left for
  `conflict` or `host_failed` and that failure still stands),
  run (none failed and one is not registered yet or still runs), or passed.
  1. reviewer, not approved, checks passed;
  2. author, approved, checks passed or running;
  3. reviewer, not approved, checks running;
  4. author, approved, checks failed;
  5. author, not approved, checks passed or running;
  6. author, not approved, checks failed;
  7. reviewer, not approved, checks failed;
  8. reviewer, approved.
  Equal ranks follow `updated_at` ascending (a push to the head counts as
  activity). The cursor locates a rank, update, and number: a pull request
  whose rank changes between pages may move past the cursor or be seen twice.
  Only `state=open` is offered; `draft` is `true` or `false` (omitted: both).
- `GET /api/v1/repos/{repo}/pulls/{number}` → pull request.
- `GET /api/v1/repos/{repo}/pulls/{number}/activity` → activity:

  ```json
  {"comments": [...comment shape...],
   "reviews": [{"id","author","verdict","body","commit","submitted_at","reply_only","reactions":[...],"url"}],
   "threads": [{"id","resolved","outdated","path","line","side","start_line","start_side","commit","comments":[...thread comment...]}]}
  ```

  `reply_only` is always `false` (a reply never creates a review). A thread is
  `outdated` when its `commit` is not the current head.
- `PATCH /api/v1/repos/{repo}/pulls/{number}`
  `{"title"?, "body"?, "tickets"?: [...], "drop_ticket_links"?: false, "draft"?: true, "revision"?}`.
  A body change keeps the stored ticket links unless `drop_ticket_links` is
  true or `tickets` is given (which replaces them). `draft: true` is the only
  draft change an agent may make; `draft: false` (ready for review) is
  the owner's. The response is the pull request.
- `POST .../pulls/{number}/comments` `{"body","meta"?}` → `201` comment; listing them is in process only.
- `POST .../pulls/{number}/reviews` `{"verdict": "approve"|"request_changes"|"comment", "body", "commit"}`
  → `201 {"id","verdict","commit","submitted_at","url"}`; `409 head_moved` when
  `commit` is not the current head. `body` may be empty. The author's own review can
  only be a `comment` (`400 invalid` otherwise). An `approve` on the current head
  counts toward the approvals a merge and a publish need (`review_states`).
  A closed pull request takes no reviews or inline comments
  (`409 conflict`). A review removes its author from `reviewers` (the request is
  answered). `GET` → items of review.
- `POST .../pulls/{number}/inline-comments` (in process only)
  `{"body","commit","path","line","side":"LEFT"|"RIGHT","start_line"?,"start_side"?}` →
  `201` thread comment (it roots a new thread); `409 head_moved` as for reviews;
  `400 invalid` when `path` is not changed by the pull request.
- `GET .../pulls/{number}/threads` → items of `{"id","resolved","outdated","path","line","side","start_line","start_side","commit","comments":[...]}`.
  A thread's id is its root comment's id.
- `POST .../pulls/{number}/threads/{id}/replies` `{"body"}` → `201` thread comment.
  `404` when `id` roots no thread of this pull request.
- `PUT .../pulls/{number}/threads/{id}/resolved` `{"resolved": true|false}`.
- `PUT .../pulls/{number}/reviewers/{name}` and `DELETE`: request or withdraw a member's review. A request
  made after the member reviewed asks for a new review.
- `GET /api/v1/repos/{repo}/commits/{sha}` → `{"sha","message","author","authored_at","parents":[...]}`.
- `POST .../pulls/{number}/reactions` `{"target_id","reaction"}`: the target is a
  conversation comment, a thread comment, or a review of this pull request.
  `DELETE` with the same body takes the caller's own reaction back → `204`.
- `GET .../pulls/{number}/files` → items of
  `{"path","previous_path","status":"added"|"removed"|"modified"|"renamed","additions","deletions","patch","patch_complete"}`
  for the diff `merge-base(base, head)..head`. `patch` is the unified diff of the
  file from its first `@@` line (`null` for binary files); it is cut at 256 KiB
  with `patch_complete: false`.
- `GET .../pulls/{number}/commits` → items of
  `{"sha","author","committed_at","subject"}`, oldest first: the commits of
  `base..head` (once merged, of what the merge brought in). Every commit here
  was pushed to Branvil. `committed_at` is the committer date in UTC, the same
  form as the service's own times.
- `POST .../pulls/{number}/merge` (owner)
  `{"expected_head": "<sha>", "expected_base": "<sha>"}` → pull request. The pull
  request must meet the shared landing conditions. A moved head or base returns
  409; blockers return `409 not_mergeable` with their codes and messages.
  A draft, a closed pull request, or a head that adds nothing to the base is
  `409 not_mergeable` too. A repository with an upstream merges there:
  `409 not_mergeable`, publish instead.
  On success the base branch advances to head if head contains base, or to a
  new Branvil commit with parents `[base, head]` and message
  `Merge !<number>: <title>` otherwise (`merge_sha`). Merge conflicts are
  `409 merge_conflict`, listing the conflicting files; nothing is written.
  In either case the pull request keeps its original reviewed `head_sha`, it is closed as merged,
  and the tickets it `closes` are closed (`completed`) and moved to `done`.
  Its head branch is deleted only while still at the original reviewed head,
  unless it is protected or an open pull request has it as its head or base.
  A pull request merged on the
  upstream host has its head branch deleted in Branvil the same way, and only
  while that branch is still at `upstream.pushed`; the host's branch stays.
  A repository with `auto_land` merges by itself once these checks pass
  ("Landing automatically").
- `POST .../pulls/{number}/close` and `.../reopen` → pull request. Reopening is
  `409 conflict` when another open pull request has the same head and base; a merged
  pull request cannot be closed or reopened.

### Landing

Each open pull request carries `landing` with the record fields described below,
plus `blockers` (objects with `code`, `message`, `files`, `jobs`), `next`,
`host_url`, and `next_detail`. Closed and merged pull requests carry `null`.
These blockers are computed response data, not persisted records. `message` remains
English for API/CLI consumers; the GUI translates `code` and the structured values.
There is no queue-list or resume endpoint; both return 404.

Landing conditions are computed for each open pull request independently by
`landing::blockers`: `draft`, `already_in_base` (including an empty head),
`conflict` (with `files`), `publish_requirements`, `head_checks` (with `jobs` naming
only unmet required checks), `approvals` (with `count` and `required`, the approval
counts used by the server),
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

`next` is chosen in this order: `following` / `owner_fix_following` for a host
merge, `host_closed`, `conditions` when blockers remain, `owner_publish` for a
manual/previous/stopped publication, `owner_fix_auto` for this head's automatic
failure, `publishing`, `host_checks`, `owner_merge_host`, or (without upstream)
`auto_merge` / `owner_merge`.

`hold_while_failing` defaults to false and is owner-editable. When enabled,
`held` is the first blocker for a non-repair PR targeting the default branch
while verification has `failing_since` and an open repair ticket exists.
Its message names the repair number and assignee; `held: {number, assignee}`
provides structured display data (other reasons have `held: null`). It prevents
publication, local manual/automatic merging and readiness notifications.
Already published PRs also show the hold, but host merging and head updates
continue. A verification success or closing the repair ticket removes the hold
on the next sweep, including a verification event.

### Landing automatically

With `auto_land`, every eligible pull request merges or publishes independently.
Sweeps run at start, every minute and on pull, check, repo, upstream and
verification events. Automatic publication is attempted only once; a previous
publication, including a stopped one, is left to the owner. Publishing a stopped
head directly replaces the host branch after checking the same conditions.

### Checks (readiness data)

`GET .../pulls/{number}/checks`

```json
{
  "head_sha": "...", "base_sha": "...", "behind_by": 0, "conflicts": [],
  "checks": [
    {"name": "test", "status": "completed", "conclusion": "success",
     "attempt_id": "att_01JA...", "attempt": 1, "runner": "runner-west",
     "details_url": "http://127.0.0.1:7878/acme/demo/-/jobs/att_01JA...",
     "artifact_names": ["report"],
     "started_at": "2026-10-24T09:50:00.000Z", "finished_at": "2026-10-24T09:53:12.000Z"}
  ],
  "checks_expected": true,
  "required_checks": "passed",
  "required_names": ["test"],
  "base_checks": 1
}
```

- `head_sha` / `base_sha` are `""` and `behind_by` is `null` when that branch is gone.
  A merged pull request's `head_sha` is the original reviewed head, as in its view.
- `conflicts` lists the conflicting file paths, sorted and deduplicated, computed
  with `git merge-tree --write-tree --allow-unrelated-histories` for this response's
  `base_sha` and `head_sha`. Independent histories are compared against an empty
  ancestor for conflict inspection; this does not relax the landing merge policy.
  It is `[]` for a clean merge and `null` for a closed PR or a missing branch.
- `checks` include the latest attempt of every job of the head's run, as the
  run's pipeline defines the job (`docs/ci.md`),
  plus entries without an `attempt_id` that say why something is missing, each
  with `details`:
  - a required check when the commit has no run yet: `status: "queued"`,
    `conclusion: null` ("No CI run is registered for this commit yet");
  - a job of a run that waits for room in the queue, or that is not queued
    yet (the reconciler queues it): `queued`, `null`;
  - a required check the run cannot have (no pipeline in the commit, an
    invalid pipeline, no job of that name): `completed`, `failure`;
  - an invalid pipeline: a check named `pipeline`, `completed`, `failure`, the
    reason in `details`;
  - a job of a superseded run that never got queued: `completed`, `cancelled`.
- Checks with execution times appear first, by `started_at` ascending (or
  `finished_at` for checks completed without starting). Untimed in-progress
  or completed checks follow. Queued Branvil attempts then follow the runner's
  current claim order: priority class, reusable results first within the class, then
  admission sequence. Jobs awaiting admission follow those attempts, in the
  admission planner's current job order; checks without a local schedule,
  including upstream placeholders, retain their relative response order.
  Every response recomputes the order without claiming work. The GUI reloads
  active check lists on CI, PR, ticket and repository events, and reloads PR lists on
  CI events too. New jobs, approvals, retries and available results can change
  this prediction before a runner claims the next job.
- `details` (optional on every check) explains a conclusion or a state: the
  failing exit code, "Superseded: …", "Interrupted: …", "No CI runner has
  contacted Branvil …", the executor's reason for `startup_failure`.
- `runner` is the member name of the runner that claimed the check's
  attempt. It is `null` for unclaimed attempts, including service skips, and
  for checks without a Branvil attempt, including placeholders and upstream checks.
- A check of a Branvil attempt has `started_at` once a runner claimed it and
  `finished_at` once it concluded (the GUI shows how long it ran, or has run
  so far). While it is `in_progress`, `expected_secs` is how long the same job
  (by name) last took from claim to conclusion where it succeeded: on the
  commit itself or the nearest of its first-parent ancestors (20 commits in
  all), excluding reused attempts; there is none without such a commit. The GUI shows the time left by
  it.
- `status` is `queued`, `in_progress`, or `completed`. `conclusion` (when
  completed) is `success`, `failure`, `cancelled`, `timed_out`, `skipped`, or
  `startup_failure` (no runner or no usable executor; the details say which).
  A service path selection also produces completed `skipped` checks, with
  `path_skip: {"reason":"paths","paths":["src/**"],"bases":[{"pull":3,"base":"main","base_sha":"<full sha>","merge_base":"<full sha>"}]}`.
  This field is omitted on other checks. Each base is the snapshot used to
  decide that none of the job's paths changed. `details` and the attempt's
  log explain the decision. Only these service skips satisfy required head
  checks; a runner's `skipped` does not. Service skips are neither reuse
  sources nor duration estimates; retry queues an actual execution.
  A successful attempt belongs to that exact commit, but may reuse an actual
  successful execution of the same tree (on a pull request's head, the same
  input digest of the job's `paths`), job definition, network setting,
  and image digest (`docs/ci.md`, "Trust"). Such a check includes
  `reused_from: {"attempt_id","url","sha","basis"}` linking its source;
  `basis` is `tree` when the source's commit has the check's tree, otherwise
  `paths` (derived, not stored). Its
  Jobs declaring artifacts reuse executions only of the same commit, since a
  distributed executable can embed its commit identity even with identical trees.
  `artifact_names` resolve to the source's unpruned artifacts. Cache refresh
  attempts are excluded from every checks response, including required checks
  while their actual executions count toward estimates.
- `checks_expected` is true when the head has a pipeline (valid or not) or
  Branvil cannot read whether it has one, or when the repository has required
  checks: a head without checks is then "not
  registered yet", never "no checks".
- `required_checks` is the landing head-check result (`passed`, `running`, or `failed`); `required_names` identifies required jobs (all head jobs for repair PRs). Upstream checks do not affect this result.
- `base_checks` counts the checks of the base tip.
- A published pull request whose head is the commit Branvil pushed also lists
  the upstream host's check runs of it, named `<provider>/<name>`
  (`github/test-windows`), with `attempt_id` `<provider>-<id>`, `details_url`
  the host's page, and `details` the host's summary. Their log is read through
  the job-log endpoint below; the other job endpoints do not know them.
  While the host may still add checks of an open pull request's head, a check
  named `<provider>` without `attempt_id` stands for them: `queued`, `null`,
  with `details` "GitHub may still add checks of this commit". The host may
  add them until Branvil reads the host after `[upstream]
  checks_registration_secs` (60 by default) passed since its push or the
  host pull request's creation, edit by Branvil, or reopening (some checks
  being there does not mean they are all). The poller wakes at that deadline
  to read the host without waiting for the next regular poll; a failed read
  leaves the wait in place and is retried at the regular interval. The host
  may also add checks while a GitHub Actions
  workflow run of the head, or another app's check suite of it that has check
  runs, has not finished (a workflow run creates a job's check run only once
  it starts the job: after the jobs it needs, its concurrency group, or an
  approval). A suite without check runs of an app other than GitHub Actions
  is not waited for: such an app may never report. A workflow run that
  failed without a check run (an invalid workflow file) is listed as a failed
  check named after the workflow, with no log.

`GET /api/v1/repos/{repo}/commits/{sha}/checks` →
`{"sha": "...", "checks": [...], "checks_expected": true, "run_registered": true}`: the checks of any
commit of the repository (the default branch's tip, for instance), `checks`
and `checks_expected` as for a pull request's head above, without the upstream
host's check runs. `run_registered` states whether a local CI run exists;
it distinguishes an unregistered commit from a registered run waiting for
queue room, including when both expose required-check placeholders. Only a commit that is or was the default branch's tip or
an open pull request's head gets a run (`docs/ci.md`,
"What registers a run"):
with a pipeline or required checks, another commit's checks stay "not
registered yet". `404 not_found` when `sha`
is not the full sha of a commit of the repository.

### CI attempts

- `GET /api/v1/repos/{repo}/jobs/{attempt_id}` →
  `{"id","purpose","cache_refresh","url","repository","job","attempt","runner","sha","image_digest","reused_from","status","conclusion","path_skip","details","cancel_requested","created_at","started_at","finished_at","artifact_names","log_bytes"}`.
  `path_skip` has the same shape as on checks and is `null` for an attempt
  that was not skipped by the service's path selection.
  `runner` is the member name of the runner that claimed this attempt, or
  `null` if it was never claimed. Each retry has its own runner.
  `image_digest` is the configuration digest (`sha256:…`) of the image the
  job ran on, as its runner reported it; `null` when the job did not get that
  far. `purpose` is `check`, `cache_refresh`, or `probe`. `cache_refresh` is the ID of
  the refresh requested by a reused check, or `null`. Refreshes never count as
  checks and cannot be manually retried. `reused_from` is `null` for an actual
  execution, otherwise
  `{"attempt_id","url","sha","basis"}` of its source execution (`basis` as in
  the checks). Its own log explains
  the reuse; the source URL leads to the execution log. `artifact_names` are
  the source's artifacts, empty after the source's retention expires.
  For automatic retries, `details` includes `Automatic retry 1/3 of att_…`
  and the attempt's own reason. Check `details` carries the same text.
  Queued attempts in classes 4a/4b append `Deferred: <reason>.` and when they
  run: `It runs when no protected-branch, near-landing or normal job is queued.`
  (4a), or `It runs when no other job is queued.` (4b). This follows any
  automatic retry explanation. Checks for runs waiting for queue capacity
  append only `Deferred: <reason>.` to the waiting message. Other classes
  and non-queued attempts keep their existing details.
  `404 not_found` for an id of another repository.
- `GET /api/v1/repos/{repo}/jobs/{attempt_id}/log?tail_bytes={n}` →
  `{"log": "...", "log_bytes": 12345, "truncated": true, "pruned": false}`; `log`
  is the last `tail_bytes` bytes (default 65536, at most 4194304; decoded as
  UTF-8 with replacement), `truncated` says the log is longer than that,
  `pruned` that retention deleted it. For an upstream check (`github-<id>`) the
  log is read from the host with the upstream's reader: `404` when the host
  has none for it, and `409 no_account`, `409 upstream_refused`, or `503
  unavailable` as for dependency alerts ("Dependency alerts").
- `POST /api/v1/repos/{repo}/jobs/{attempt_id}/retry` → the new
  attempt (`200`, `status: "queued"`, the job's next `attempt` number, the
  same job definition). Only the latest check attempt of its job as the commit's
  pipeline defines it, once completed, can be retried (`409 conflict`
  otherwise, also for an attempt of another definition). Cache-refresh attempts
  always return `409 conflict` with "Cache refresh attempts cannot be manually
  retried.", whatever their status; a full queue is
  `503 unavailable`. The new attempt always executes; it never reuses a
  result, including when requeued after interruption. A manual retry starts
  a fresh automatic retry budget. Retry waits for ongoing
  repository ref changes,  before it
  queues the new attempt.
- `POST /api/v1/repos/{repo}/jobs/{attempt_id}/cancel` → the attempt: a
  queued one is `completed`/`cancelled` at once, a running one gets
  `cancel_requested: true` and is concluded `cancelled` when its runner stops
  (or its lease runs out). A completed attempt is returned unchanged.
- `GET /api/v1/repos/{repo}/commits/{sha}/artifacts/{name}` →
  `application/zip` with `Content-Length`; the artifact of the newest check attempt
  of that commit that has it, resolving a reused result to its source's file.
  The source's original retention applies. `404 not_found` when there is none.

## Runner API (`/api/v1/runner`, runner token)

Used by `guildbotics runner` only. A runner token is a member token of a member of
kind `runner` (created by `guildbotics runner register`); any other member gets
`403 forbidden`. The lease of a claimed attempt is sent in the JSON body
(`heartbeat`, `finish`) or in the `X-Branvil-Lease` header (`source`, `log`,
`artifacts`), never in the URL.

Claims select protected-branch work, near-landing work, normal work, deferred
work, then last-class work, deriving the class anew from store records. Within
one class, available reusable results precede other attempts, with `seq` as the
tie-breaker. See [CI scheduling](ci.md) for the classification rules. Running
attempts are not interrupted by changes in class.

- `GET /health` → `{"version","max_jobs"}`, the Hub build and the authenticated
  runner's effective concurrency. The runner checks for a version change before
  it claims.
- `POST /claim` `{"architecture":"arm64"|"amd64","version","max_jobs","running_jobs","accept":true|false,"executor":"microsandbox","executor_state":"ready"|"unavailable","executor_detail"?:"...","images":[{"reference":"tag","architecture":"arm64"|"amd64","digest":"sha256:..."}]}`
  records a report even when there is no work or `accept` is false. The
  runner reports its build, the concurrency it read, and the number of locally
  held attempts, including when idle or at capacity. The Hub returns `204`
  when the runner did not ask for work, the version differs, the executor is
  unavailable, the Hub's current concurrency has been reached, no compatible
  attempt is queued, or the service is stopping. Otherwise it returns:
  `{"attempt_id","purpose","lease","expires_at","lease_secs","repo","sha","attempt","verified_sha":"<latest verified sha>"|null,"job":{"verified_sha":false,"name","image","commands","paths","timeout_secs"|null,"artifacts":[{"name","paths"}],"cache":{"paths","key_files"}|null},"cache_scope":"protected"|"branch:<name>"|"probe","network":false,"queued_secs","reuse","limits","artifacts_max_mib"}`.
  `limits` contains the effective job resources for this attempt (see
  [CI settings](#ci-settings)). `timeout_secs` is `null` when the pipeline
  gives none. `cache_scope` is `protected` for a run a protected branch asked
  for, the only runs that save the dependency cache (`docs/ci.md`).
  `queued_secs` is how long the attempt waited in the queue.
  `purpose` is `check`, `cache_refresh`, or `probe`; refreshes always execute with
  protected scope and `reuse: null`.
  `reuse` is `null` or `{"attempt_id","sha","image_digest","cache_key","basis"}`
  of the latest finished actual successful execution whose files have not
  been pruned, with the same repository, job definition, and network, and
  with the same tree (`basis: "tree"`) or the same input digest of the job's
  `paths` (`basis: "paths"`, `docs/ci.md`, "Trust": only for a non-protected,
  attempt of a job with paths whose run has its digest). The runner
  logs which one it reused. `cache_key` is present for cached jobs,
  otherwise `null`. Manual retries, automatic retries, and refresh attempts
  receive no reuse offer.
  The runner compares the current image digest before fetching source. A match
  succeeds without a VM. Protected cached jobs set `refresh_cache: true` at
  finish if the save is absent, unreadable, or older than 24 hours. The executor
  state appears in diagnostics. Runners advertise only locally cached images for their CPU;
  the service selects the highest-priority compatible attempt, leaving incompatible work queued.
  Hub-managed tags must match the currently published digest for that CPU. An unavailable
  executor advertises an empty inventory. If no live, ready runner has the image for
  `runner_absent_secs`, queued work ends with `startup_failure` naming the missing tag and CPU.
- `GET /images?architecture=arm64|amd64` → an array of
  `{"id":"<archive SHA-256>","images":[{"reference","architecture","digest"}]}`.
  Only archives for that CPU are returned. `digest` is the image configuration digest,
  preserved by `docker save` and `msb load`.
- `GET /images/{id}` → the immutable tar archive. Both image endpoints require a
  runner token. Runners compare local configuration digests before downloading and
  load changed archives under the image-cache lock before advertising them.
  Synchronization runs independently of claims: downloads do not hold that lock,
  and one failed archive does not prevent other archives or cached jobs.
  Runners report synchronization progress/errors in `executor_detail`, including
  while all slots are occupied (`accept:false` updates the report without leasing work).
- `GET /attempts/{id}/source` → `application/x-git-bundle`: a v2 Git bundle of
  the commit as `HEAD`, with its history and the tags (`refs/tags/*`) whose
  commits it reaches.
- `POST /attempts/{id}/heartbeat` `{"lease"}` → `{"cancel": false, "expires_at"}`
  (renews the lease).
- `POST /attempts/{id}/log` raw bytes (at most 1 MiB per request) appended to
  the log → `{"log_bytes", "truncated"}`; past `ci.log_max_mib` the log ends
  with a truncation notice and later bytes are dropped.
- `PUT /attempts/{id}/artifacts/{name}` zip bytes → `{"name","bytes"}`. The name
  must be one the job declares; `400 invalid` for anything not starting like a
  zip archive, `413 too_large` above `ci.artifact_max_mib`.
- `POST /attempts/{id}/finish` `{"lease","conclusion","details"?,"image_digest"?,"reused_from"?,"cache_key"?,"refresh_cache"?}`
  → the attempt. `image_digest` is the configuration digest of the image the
  job ran on (`sha256:<64 hex>`, `400 invalid` otherwise), recorded on the
  attempt. An actual execution of a cached job reports its computed
  `cache_key` (64 hex characters); other jobs leave it `null`. A reused success
  reports the source attempt ID as `reused_from` and leaves `cache_key` and
  `details` null; the source is already identified by `reused_from` and the log.
  `refresh_cache` defaults to false and is allowed only for a reused successful
  protected job with a cache. When true, the same transaction enqueues a
  `cache_refresh` attempt, or links the existing unfinished refresh for the
  repository/job/cache key. A full queue skips it without delaying success.
  The check includes `cache_refresh` (the attempt ID) and a link in `details`
  when a refresh exists.
  The service validates that the source is an actual successful execution of
  the same repository, job, network, and image, with the same tree or input
  digest as the claim's `basis` allowed, and that reuse is allowed;
  an invalid reuse report is `400 invalid`. When a cancellation was requested
  no reuse is recorded and the conclusion is `cancelled`
  whatever was reported. A `paths` reuse reported after a protected branch
  came to want the commit, from a source of another tree, concludes
  `cancelled` (`Stopped: the commit became a protected branch's; it runs again
  as one.`) and queues a new attempt in its place.

A request with a lease that is not the attempt's current, unexpired lease
(another runner's, expired, cancelled, re-queued) is `409 conflict` and changes
nothing.

## Events (GUI)

`GET /api/v1/events` is a Server-Sent Events stream. Each event is
`event: change` with `data: {"kind": "ticket"|"pull"|"board"|"check"|"upstream"|"repo", "path": "...", "id": "...", "actor": "owner"|"agent"|"runner"|null}`.
`path`/`id` by kind: `ticket` repository / key, `pull` repository / number, `board` workspace / —, `check`
repository / commit sha, `upstream` repository / — (what following its upstream found changed), `repo` repository / — (its settings, its upstream, or its refs changed, or a member's account on its upstream host was set). The actor is the authenticated writer’s member kind, or null for system changes. Events only say what changed; the GUI refetches it. On reconnect (or after a
`event: reset`, sent when the client fell behind) the GUI refetches what it shows.
The stream ends when the service shuts down. The service subscribes to the
same changes itself to land pull requests automatically ("Landing
automatically").

The GUI shares one credentialed stream across tabs of the same origin: a Web
Lock elects the tab that owns it, and a BroadcastChannel distributes changes,
resets, and connection state. Closing that tab releases the lock so another
tab connects. Each tab refetches its shown queries when it joins a connected
stream, when the stream reconnects or changes owner, and on reset. All tabs
warn after the shared connection has been down for five seconds. Navigating
away releases participation; a page restored from the back/forward cache
rejoins and catches up. This requires Web Locks and BroadcastChannel in the
browser (the service's loopback HTTP origin is a secure context).

## Administration (owner)

Everything here needs the owner, except reading what publishing would post
and polling now: these endpoints store tokens, make the service talk to other
hosts, or write files on the service's machine.

### Upstream

Pull requests targeting a nondefault branch merge inside Branvil, including in
repositories with an upstream. They need no upstream author account and are
not published automatically. A `closes` link closes tickets only on a merge
into the default branch; feature-branch merges leave tickets open.

A repository may follow a repository on a code host (GitHub today), which
stays the place of record: Branvil's default branch moves only to the host's,
and pull requests are published there, as their authors, to be merged by
people. Conversation, reviews, and threads stay in Branvil. The design, what
is polled when, and how conflicts are settled: `docs/architecture.md`,
"Upstream".

- `PUT /api/v1/members/{name}/accounts/{provider}` (owner) → member, with one
  of two bodies:
  - `{"token": "..."}`: a token of the account itself. The host is asked who
    it is (GitHub: `GET /user`).
  - `{"app": {"app_id": "123", "installation_id": "456", "private_key": "-----BEGIN RSA PRIVATE KEY-----\n..."}}`:
    a GitHub App, installed on the account that owns the repositories. Branvil
    signs a JWT with the key (RS256, ten minutes), reads the app's slug
    (`GET /app`), and makes an installation token
    (`POST /app/installations/{id}/access_tokens`); the login is
    `<slug>[bot]`. Installation tokens are made whenever one is needed and
    kept only in memory.

  `400 invalid` when the host refuses the credential (a wrong key, an unknown
  installation) or the body is neither form, `503 unavailable` when the host
  cannot be reached. The login and the kind (`token` or `app`) are kept on the
  member and the credential in a `0600` file, never returned. Setting an
  account replaces the member's account on that host, whatever its kind. A
  runner has no accounts. `DELETE` removes it.
- `PUT /api/v1/repos/{repo}/upstream` (owner)
  `{"provider": "github", "repository": "owner/name", "reader"?: "<member>" | null}`
  → repository. `reader` is whose account reads the upstream: a member with an
  account on that host (`409 no_account` otherwise), `null` to pick one again,
  absent to keep the current one (or, for a new upstream, to pick one). A
  picked reader is an agent's account (the first by name), the owner's only
  when no agent has one (`409 no_account` when nobody has one); it stays until
  the owner changes it or its account is removed (then another is picked at
  once). The host's default branch is read first; Branvil's
  becomes it when Branvil's is missing or behind (`409 conflict` when
  Branvil's has commits the host's lacks; `503 unavailable` when the host
  cannot be read). Changing it and `DELETE` (→ repository without upstream)
  are always allowed: a pull request published before keeps talking to the host
  repository it was published to, and removing the upstream stops following it
  and polling its published pull requests.
- The `upstream` of a repository:

  ```json
  {"provider": "github", "host": "GitHub", "repository": "acme/demo",
   "url": "https://github.com/acme/demo",
   "reader": {"member": "aiko", "chosen": false},
   "following": {"tip": "<40 hex>", "checked_at": "...", "followed_at": "...",
                 "error": null, "stopped": null}}
  ```

  `reader.chosen` says the owner named it (`false`: picked); `reader.member`
  is `""` when no member has an account there. `following` is what the last
  poll found (kept in memory: empty after a
  restart until the first poll): the host's tip of the default branch, when it
  was checked and when Branvil's branch last moved to it, the last failure
  (retried at the next poll), and why following stopped (Branvil's branch has
  commits the host's lacks).
- `POST /api/v1/repos/{repo}/upstream/refresh` → repository: polls now (the
  default branch and every published pull request of the repository) and
  answers with what it found.
- `GET /api/v1/repos/{repo}/pulls/{number}/publish` →
  `{"host","repository","head","base","title","body","account","login"}`: what
  publishing would post, as whom (`login` is `null` when the author has no
  account on the host). `title` and `body` are the pull request's own with
  Branvil's references rewritten for the host (`docs/architecture.md`,
  "Branvil's references on the host"), and `body` gets a line for each linked
  ticket that names an issue there (`Closes #12` for a link that closes,
  `Refs #12` otherwise).
- `POST /api/v1/repos/{repo}/pulls/{number}/publish` (owner)
  `{"expected_head", "expected_base"}` → pull request. The checks of a merge
  apply (`409 head_moved`, `base_moved`, `not_mergeable`), the base must be
  the default branch, and the author needs an account on the host
  (`409 no_account`). The head branch is pushed to the host with the author's
  account and a pull request is opened there (`503 unavailable` when that
  fails; the branch stays and Branvil tries again). When the host already has
  a branch of that name that Branvil did not push, nothing is pushed: `409
  upstream_refused`, and the pull request's `upstream.stopped` says why.
  Publishing to this upstream rechecks the shared landing conditions. An already
  published PR wakes the poller; stopped sending is directly replaced by the
  owner's publish. A publication to an earlier upstream is published anew here.
  A repository with `auto_land` publishes by itself, once ("Landing
  automatically").
- The `upstream` of a pull request:

  ```json
  {"provider": "github", "repository": "acme/demo",
   "account": "aiko", "number": 42, "url": "https://github.com/acme/demo/pull/42",
   "state": "open", "pushed": "<40 hex>", "published_at": "...",
   "error": null, "stopped": null, "auto": false}
  ```

  `provider` and `repository` are where it was published. `state` is the
  host's: `none` (no pull request there yet), `open`, `closed`, or `merged`. `pushed` is what Branvil
  last pushed. `error` is the last failure (retried at the next poll);
  `stopped` says why sending the branch stopped (the host has a commit Branvil
  did not push, or refused the update). `auto` says Branvil published it by
  itself (the owner's publish makes it `false`). Notification delivery is held by `landing.notified_at`.

### Webhook

Branvil tells the webhook when an automatically landing pull request is ready to merge on the host, when a
repair ticket is created after main verification fails, and when a scripted service
update or activation stops. Its URL is a credential of another system:
it is kept in the service's vault (not in the data directory), never returned,
and not in backups.

- `GET /api/v1/admin/webhook` → `{"set": true, "host": "hooks.slack.com",
  "problem": null}`: whether one is set, the host of its URL, and why the last
  send failed (kept in memory; retried at the next look).
- `PUT /api/v1/admin/webhook` `{"url": "https://..."}` → the same. `400
  invalid` unless it is an `http` or `https` URL. It replaces the one set.
- `DELETE /api/v1/admin/webhook` → the same, with `set: false`.

For every automatically published PR in an `auto_land` repository whose `next`
is `owner_merge_host` and `notified_at` is empty, Branvil sends:
`{"text":"<title> (<repo> !<n>) is ready to merge on <host>.\n<Branvil PR URL>\n<host PR URL>"}`.
Successful delivery sets `notified_at` once per head. Failed delivery is retried;
a crash between delivery and recording may repeat the notice. Unanswered
statements block it. Manual publications and local merges send no such notice.

Required protected checks automatically retry command failures, timeouts,
runner absence, and second interruptions up to three times, keeping the same
commit and definition. Other startup failures, requested cancellation,
supersession, optional jobs, PR runs, and cache refreshes are excluded.
The failed attempt and queued retry commit atomically; commit checks show
the retry as queued. Retries caused by runner absence wait until claimed.

Creating a repair ticket atomically stores one webhook notice containing the
repository, branch, failed commit and jobs, verified-to-failed range, PR numbers,
ticket URL and assignee. Delivery clears `Ticket.repair.notice`; an unset or
unavailable webhook leaves it indexed for the next sweep (events, startup and
every minute). Ordinary restart does not repeat delivery; a crash between sending
and recording may repeat it. Existing tickets gain `notice: null` on upgrade.
Further failures on an open repair ticket, closing it, required-check failures
alone and automatic retry recovery do not send notices.

### CI settings

What the owner changes under Settings → CI (`docs/ci.md`). The runner mode and
the limits are `branvil.toml` settings: written there, keeping the rest of the
file, and applied without a restart. The image cache is the supervised
runner's (`[ci.microsandbox] home`).

- `GET /api/v1/admin/ci` (owner) →

  ```json
  {"runner": {"mode": "auto", "state": "ready", "detail": "1 runner(s) connected.", "runners": [...], "queue": {...}},
   "executor": {"msb": "/…/msb", "problem": null, "home": "/…/.branvil/msb-home"},
   "limits": {"max_cpus": 4, "max_memory_mib": 4096, "work_quota_mib": 16384, "max_timeout_secs": 3600, "cache_max_mib": 2048, "max_jobs": 1},
   "images": {"error": null,
              "cached": [{"reference": "guildbotics/agent-environment:dev",
                          "digest": "sha256:…", "size_bytes": 3701474304, "created_at": "…",
                          "used_by": [{"repository": "acme/demo", "jobs": ["test"]}]}],
              "missing": [{"image": "…", "repositories": [{"repository": "acme/web", "jobs": ["lint"]}]}]},
   "operation": null,
   "repositories": [{"path": "acme/demo", "ci_network": false}]}
  ```

  `runner` includes the diagnostics' runner section (below). Each row adds a
  `missing_images` array of tags named by default-branch pipelines without a
  matching image in the runner's last reported inventory for its CPU. The
  field is `null` before the first report. Top-level `limits` are the Hub
  defaults from `[ci.executor]`. `executor.msb` is the
  msb the runner would use (`null` with the reason in `problem`). `cached` are
  the images of the cache; `reference` is the tag a pipeline writes as
  `image` (`docs/ci.md`), and `digest` the configuration digest of the
  version the cache holds under it; `used_by` are the repositories whose
  default-branch pipelines run it, with their jobs. `missing` are the images
  those pipelines name that the cache does not hold. `operation` is the
  latest image pull or load:
  `{"kind": "pull"|"load", "target", "state": "running"|"succeeded"|"failed",
  "detail", "started_at", "finished_at", "loaded": [{"reference"}]}`.
- `PUT /api/v1/admin/ci/runner` (owner) `{"mode": "auto"|"off"}` → the settings.
  `auto` starts the supervised runner, `off` stops it.
- `PUT /api/v1/admin/ci/limits` (owner) `{"max_cpus", "max_memory_mib",
  "work_quota_mib", "max_timeout_secs", "cache_max_mib", "max_jobs"}` → the
  settings; `400 invalid` outside 1–64, 128–65536, 1024–1048576, 1–86400,
  0–1048576, 1–64. Every job runs with `max_cpus` and `max_memory_mib`;
  `work_quota_mib` is what a job may write to `/work` beyond the checkout and
  the restored cache; `max_timeout_secs` is the time limit of
  a job that gives no `timeout_secs` and caps one that does; the runner runs up
  to `max_jobs` jobs at once. The runner applies them from its next claim; the
  jobs it is running keep theirs. This endpoint updates the Hub defaults.
- `PUT /api/v1/admin/ci/runners/{name}/limits` (owner) takes the same six
  fields and returns the settings. It saves an override under
  `[ci.runner_limits.<name>]` for the named runner member, including members
  that have never reported. An unknown name or a member of another kind returns
  `404`. Each runner without an override uses the Hub defaults.
- `POST /api/v1/admin/ci/images/pull` (owner) `{"reference"}` → `202` with the
  settings, `operation` running; `400 invalid` for a reference that is not
  `[registry/]name[:tag]` (pipelines name an image by its tag, so a digest
  is refused). Pulling a tag again brings its new version, if any.
- `POST /api/v1/admin/ci/images/load` (owner) `{"path"}` → `202` likewise. The
  path is absolute and names a regular file (a `docker save` / OCI archive) on
  the service's machine; `400 invalid` otherwise. The Hub copies the archive into
  `<data>/ci/images`, indexing tags by CPU and configuration digest. Archives must
  contain one Linux image configuration for one CPU (arm64 or amd64), with one or
  more tags. Foreign-CPU archives are stored
  for distribution and never loaded into the Hub runner cache; native ones are also loaded.
- The mode or limits already in effect change nothing (the runner goes on).
  A configuration file that cannot be updated (not TOML, or no longer a
  configuration once changed) is `409 conflict` and stays as it is.
- One image operation at a time: another one while it runs is `409 conflict`.
  `loaded` names the images the operation brought, new or already cached:
  the pulled reference, or the references the archive carries (`RepoTags` of
  a `docker save` manifest, the image names of an OCI index).

### Backups

- `POST /api/v1/admin/backup` `{"to": "/absolute/new/dir"}` → the backup's manifest (see
  `docs/backup-restore.md`). The directory is on the service's machine, must not exist or be
  empty (`409 conflict` otherwise), and must be outside the data directory (`400 invalid`).
  `guildbotics server hub-data backup --to <dir>` calls it for the code host's part.

### Diagnostics

- `GET /api/v1/admin/diagnostics` (owner) →

  ```json
  {"service": {"name": "branvil", "version": "0.1.0", "base_url": "http://127.0.0.1:7878"},
   "storage": {"engine": "redb", "path": "/…/branvil.redb", "file_bytes": 565248, "schema_version": 9,
               "cache_size_bytes": 33554432, "cache_used_bytes": 0, "cache_read_hits": 0,
               "cache_read_misses": 0, "pending_ref_operations": 0,
               "queues": {"writes": {"queued": 0, "queue_on_arrival": {...}, "wait_us": {...},
                                     "hold_us": {...}, "commit_us": {...}},
                          "reads": {"queued": 0, "wait_us": {...}, "hold_us": {...}}}},
   "git": {"version": "git version 2.54.0", "repositories": 1},
   "runner": {"state": "ready", "detail": "1 runner(s) connected.", "mode": "off",
              "version_mismatch": null, "runners": [{"name": "runner1", "connection": "online", "last_seen_at": "…",
                           "executor": "microsandbox", "executor_state": "ready", "executor_detail": null,
                           "version": "…", "version_matches": true, "architecture": "arm64",
                           "running_jobs": 1, "max_jobs": 1, "missing_images": null, "limits": {...},
                           "limits_source": "default"}],
              "queue": {"queued": 0, "in_progress": 1, "waiting_runs": 0, "limit": 256}},
   "upstream": {"state": "not_configured"}}
  ```

  `pending_ref_operations` counts journaled ref changes not yet confirmed (see
  `docs/architecture.md`); it is `0` after a clean start. `queues` shows how
  storage work queues behind the single writer and the bounded readers since
  the service started: `queued` is the transactions waiting or running now;
  `queue_on_arrival` how many writers were already queued when a writer
  arrived; `wait_us` the microseconds until the writer (or a reader slot) was
  granted; `hold_us` how long it was held (the transaction's work and, for
  writes, its commit); `commit_us` the commit alone (the durable sync). Each is
  a histogram `{"count", "sum", "max", "buckets": [[largest value, count], ...]}`
  with only non-empty buckets, each about 19% wide (values below 4 have one
  each; every power-of-two range above is split into four), so the difference
  of two snapshots is the histogram of the requests in between
  (`docs/performance.md`). `runner.state` is
  `ready`, `no_runner` (no runner contacted the service within
  `ci.runner_absent_secs`), `executor_unavailable` (no connected runner has
  both a ready executor and the Hub build), or `stopping` (the service got
  SIGTERM: it gives out no job and waits for the running ones, `detail` says
  until when; `docs/ci.md`); `mode` is the `[ci] runner` setting. The list is
  built from runner members. A runner is `online` until
  `ci.runner_absent_secs` elapses without a report, `offline` after that or
  when the Hub stops its runner, and `never_seen` if it has not reported
  since the Hub started. A stopped runner retains its last report during the
  current Hub process. Only online runners with a ready executor and the Hub
  build make the aggregate state `ready`. When the state is `executor_unavailable`,
  `detail` says why. If a connected runner reported a different build,
  `version_mismatch` is its `{name, version}`; otherwise it is `null`.
  `running_jobs` is the number of attempts marked in progress for that member.
  `max_jobs` is the reported capacity and `limits` are the current effective
  job resources. `limits_source` is `default` for the Hub defaults or `runner`
  when an override is saved, even if its values match the defaults.
  `missing_images` is computed only in Settings → CI.
  `upstream` is `{"state": "not_configured"}` without
  repositories that have an upstream, otherwise

  ```json
  {"state": "problems", "repositories": 1,
   "problems": [{"repository": "acme/demo", "problem": "GitHub refused the token: ..."}]}
  ```

  (`state` is `following` without problems).

### Yield a PR head's running checks

`POST /api/v1/repos/{repo}/pulls/{number}/checks/yield` (in process only) takes no body.
Only the PR author may call it (member session/Bearer); other members, including
owners, receive `403 forbidden`. Visibility uses the usual repository and PR
rules (`404` for missing/invisible items).

Returns `{"head":"<sha>","yielded":[<attempt>]}`, using the job attempt view.
A closed PR, absent run, or run above the deferred class returns an empty list.
The run's best class across all requesting branches and PRs decides eligibility,
including drafts and required-check failures. Current running check attempts
that are neither cancelling nor superseded are cancelled and replaced atomically
with queued attempts at their original queue positions; queue capacity is not
rechecked. Queued attempts and cache refreshes are untouched. Cancellation of an
old attempt does not retry or replace the new current check. Repeated calls only
affect attempts running at the time of each call. Check events announce changes.


### Default branch verification

`GET /api/v1/repos/{repo}/verification` accepts an owner or member token or a
session. It returns `branch`, `tip`, `state`, `ahead`, `latest_verified`,
`current`, `failing_since`, and `repair`. `repair` is null without an open repair
ticket, otherwise `{number, assignee, url}` (assignee may be null). Internal consumers use `ci::verify::main_state`.
`tip` and `ahead` are null without the branch. `latest_verified` is null until
verification first succeeds, then contains `sha` and `verified_at`. `current`
is null without a record; otherwise it contains `sha`, `base`, `state`,
`started_at`, `finished_at`, `failed_jobs`, and `retry: {number, max: 3}`.
The retry number is the maximum automatic retry number among the run's
job attempts, including queued retries (zero before any retry).

The first applicable row determines `state`:

| State | Condition |
| --- | --- |
| `empty` | Default branch absent |
| `verifying` | Current record running |
| `failing` | Current record failed |
| `verified` | `ahead` is zero |
| `pending` | Otherwise |

`ahead` counts first-parent commits in `latest_verified.sha..tip`, or the whole
first-parent history of tip when no commit has been verified.

`GET /api/v1/repos/{repo}/verification/commits` has the same authentication.
It returns a cursor page (30 items by default), newest first on the default
branch's first-parent history. Each item has `sha`, `title`, `author`,
`authored_at`, and `mark`. The mark is `verified` for a verified ref, otherwise
`verifying` for the running verification commit, otherwise `failed` for the
failed verification commit or `failing_since`, otherwise null. Cursors use the
existing signed, caller- and collection-bound protocol. Pagination continues
from the last commit's first parent even if the branch advances.

The `verification` change event has the repository `path` and an empty `id`.
It is published after a verification record transaction, and when its checks
change (including automatic retries). Clients refetch verification and history.
The existing commit and pull request checks responses are unchanged.

Repository `post_merge_jobs` lists job names to execute only in protected-branch
verification. It defaults to `[]` and has the same name validation as `required_checks`.
An owner PATCH is atomic: if the resulting lists overlap, it returns 400 without
changing the record, including when only one list is supplied. Other callers get 403.
Service skips expose `path_skip.reason` (`paths` or `post_merge`). Both are completed
`skipped` checks, cannot supply reusable results or duration estimates, and are
executed by manual retry and protected registration.

Diagnostic `probe` attempts use the commit's own job definition, never reuse or
save caches, and cannot be manually retried. Their job and log routes are the
same as other attempts. They are excluded from commit checks and readiness.

PR responses, including patrol lists, expose `repair: true|false`. It is true
only for open PRs linked locally to the repository's open repair ticket. Closing
links never close repair tickets on merge; passing main verification or a human
operation closes them. Repair ticket creation and closure also publish a
`verification` event so clients refetch the linked ticket.

Board cards accept `repair=true` to select repair tickets independently of assignee (with the usual state and lane filters).

### Repair hint comments

Branvil adds diagnostic `Hint:` comments to open repair tickets when failed-job
narrowing finds the first failing commit, fails again at the last verified
commit, or observes inconsistent passing/failing results. Comments include
`meta: {"repair_hint": {"sha": "<failed commit>", "job": "<job name>"}}` and
are unique per failed commit and job on that ticket. They do not change the
assignee, lane, rank, or repair record. Conclusions available before creation
are attached in the ticket creation transaction; closed tickets receive none.
Undetermined and abandoned searches produce no hint.


Embedded hosts may attach a device-local Hub status callback through `DataApi::attach_hub_status`. Diagnostics returns its result as `hub` (`null` for standalone Branvil): `current` and `target` versions (`sha`, `subject`, nullable `url`) and nullable `operation` (`kind`, `phase`, `since`, `commit`, `waiting`). Branvil supplies `operation.waiting.ci_jobs` from its own queue. `hub_status_changed` events invalidate diagnostics. This state is never persisted.
