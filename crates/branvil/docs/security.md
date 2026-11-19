# Security

Branvil is a single-user service: one person, the owner, runs it on the
GuildBotics Hub machine under their own OS account, and the clients are that
person's browser, GuildBotics (its agents act as Branvil members whose kind
fixes what they may do), Git clients, and the `branvil` CLI. Whoever can run commands as the
Hub's OS user is the owner: the owner's token is a file in the data directory,
and the GUI is entered with a link that file's token creates. This document says what Branvil defends
against, how, where the code and tests are, and what it does not protect.
Every claim below was checked against the code of this build; the test that
covers it is named in brackets.

## Threat model

```
other machine                                 Hub machine (Branvil's OS user)
browser / GuildBotics / git ─ 127.0.0.1:7878 ─ ssh -L ═══ sshd ─ 127.0.0.1:7878 ─ guildbotics server start
                              (OS service)                                        ├─ <data>/ (0700)
                                                                                  └─ git children
```

| Who | Can | Branvil's defence |
| --- | --- | --- |
| A web page in the user's browser | Send requests to `127.0.0.1:7878` (CSRF), or to an attacker name that resolves to 127.0.0.1 (DNS rebinding) | Host check, Origin check, `SameSite=Strict` cookie, CSRF header, no CORS |
| Another process or OS user on the Hub or a forwarding client | Connect to the loopback port | Every request authenticates (a member's token, GuildBotics' owner token, or GuildBotics' browser session); there is no anonymous access |
| A member (an AI agent) | Use its token within what its kind may do | Fixed rights per member kind (owner-only settings, merges, and administration), protected branches, server-side hooks, path and name validation, entry ids bound to their item |
| Repository content | Contain hooks, Markdown, odd ref names | Repository hooks never run; Markdown is sanitized by the GUI; only `refs/heads/*` and `refs/tags/*` can be pushed |
| An external Git host | Diverge, refuse, or be unreachable | Branvil never force-pushes, never deletes there, never overwrites local refs from it |

Out of scope: anyone who runs code as Branvil's OS user or as root (they can
read the data directory and process environments), a compromised Hub machine,
and network attackers between machines (OpenSSH protects the connection; Branvil
itself speaks plain HTTP on loopback only).

## Network exposure

- The GuildBotics server listens on loopback only and serves this service on
  its listener; other machines reach it through OpenSSH port forwarding
  managed by an OS service.
- GuildBotics checks `Host` and `Origin` on every request before this
  service sees it (`gb-server`'s guard; its route classification tests cover
  every route of this service, `route_inventory()`): another host is
  `421 misdirected`, another origin `403`. The hook callbacks are the exception:
  their per-process secret authenticates them.

## The owner's authentication

GuildBotics authenticates the owner and this service does not depend on a
GuildBotics crate: the host implements `host::Host`. A Bearer value that is
not a member token (`bvt_...`) is the owner when the host says it is its
owner token (the token in `GUILDBOTICS_HOME/data/server/<port>/owner.token`;
consent tokens of other apps are refused). A browser request is the owner's
when it carries GuildBotics' live session cookie
(`guildbotics_session_<port>`, 30 days); every non-GET request made with it
must send that session's CSRF value in `X-GuildBotics-CSRF` (a missing or
wrong one is `403 csrf_mismatch`). A host session maps only to the owner;
members' Bearer tokens keep identifying their members and never fall back to
a browser session. This service keeps no sessions and no owner token of its
own [unit `auth::tests::the_host_identifies_the_owner_and_member_tokens_their_members`,
`tests/security.rs` `the_hosts_session_and_owner_token_identify_the_owner`].

## Members, tokens, sessions

- A token is `bvt_<id>_<secret>` with 32 random bytes of secret. Only the
  SHA-256 of the secret is stored, and it is compared in constant time
  (`src/auth.rs`). It is shown once, at issue. Revoking deletes the record:
  the next API request and the next Git request with it are refused
  [`tests/security.rs` `a_revoked_token_no_longer_opens_the_api_or_git`].
  Token issuance ignores `Idempotency-Key`, so the token text is never kept in
  the idempotency table [same test, which scans the database file].
- A member's tokens of one label can be replaced in one write
  (`"replace": true`), so a retried issue leaves exactly one. The label
  the service issues itself (`auto`) is refused by the tokens endpoint, so no
  client mints or rotates the automatic runner's token through it
  [`tests/security.rs`
  `replacing_a_label_leaves_one_token_of_it_and_reserved_labels_are_refused`].
- The embedding host (the GuildBotics server) shares Branvil's process and
  OS user, so `api::DataApi` trusts it without a token: `DataApi::owner()`
  acts as the owner and `DataApi::agent(name, display_name)` as an agent,
  creating it when missing and refusing a member of that name that is not an
  agent. Each operation reads the member again, so it acts with the member's
  current kind. For an agent's Git transfers the host issues a token of that
  agent for the run (label `guildbotics-git`) and revokes it when the run
  releases the service.
- What a member may do follows from its kind alone (`src/auth.rs`), the same
  in every workspace; there are no per-workspace grants (Branvil has one
  person). The owner may do everything. An agent reads every workspace and
  repository and writes tickets, comments, reactions, reviews, pull requests,
  CI retries and cancels, and unprotected branches. Owner-only operations
  (creating workspaces and repositories, repository settings, members,
  diagnostics, upstreams and members' accounts, the notification webhook,
  backup, entry links, merging,
  publishing, marking a pull request ready, the first push of a protected branch, moving or deleting a
  tag) refuse an agent with `403`. A runner gets `403` everywhere outside the
  runner API except `/me`, its own member record, and its own tokens, and
  pushes and fetches nothing
  [`tests/security.rs` `member_kinds_decide_what_a_member_may_do`,
  `a_member_is_read_by_the_owner_or_itself`, `tests/pulls.rs`, `tests/tickets.rs`
  `cursors_ids_and_member_kinds_are_checked`, unit
  `protected_branches_change_only_by_merge`].
- Collection cursors are HMAC-signed and bound to the collection, the caller,
  and the conditions; entry ids (comments, reviews, threads) are accepted only
  with their own item [`tests/tickets.rs`, `tests/pulls.rs`
  `entry_ids_belong_to_their_pull_request`].

## Git

- Smart HTTP needs HTTP Basic with the member's name and one of its tokens for
  both fetch and push; a name that is not the token's member is refused
  (`src/smart_http.rs`) [`tests/git_http.rs`
  `clone_fetch_and_push_need_the_members_token`].
- `git http-backend` runs with an argv array, an environment built for the
  request (the service's `GIT_*` removed, `GIT_CONFIG_NOSYSTEM=1`, an empty
  global configuration), `PATH_INFO` from the stored repository path, and
  `core.hooksPath` forced through `GIT_CONFIG_COUNT`, so a repository's own
  hooks never run [`tests/git_http.rs` `repository_hooks_never_run`].
- Branvil's `pre-receive` asks the service, with a per-process 32-byte secret
  from the Git child's environment, whether the member may push; it fails
  closed when the service cannot be reached. Only `refs/heads/*` and
  `refs/tags/*` are accepted (`refs/branvil/*`, `refs/pull/*`, … are refused);
  a protected branch (the default branch always) is never pushed, deleted, or
  force-pushed, except its first creation by the owner; only the owner moves or
  deletes a tag (`src/refs.rs` `check_push`) [`tests/git_http.rs`
  `protected_branches_and_internal_refs_refuse_pushes`, unit
  `protected_branches_change_only_by_merge`]. The hook callbacks refuse a
  missing or wrong secret [`tests/security.rs`
  `hook_callbacks_need_the_per_process_secret`].
- Repositories carry `receive.fsckObjects=true`, `transfer.hideRefs=refs/branvil`,
  and `uploadpack.allowAnySHA1InWant=false`. Hidden refs are not advertised and
  cannot be pushed, but they are not a confidentiality boundary: with Git
  protocol v2, a member who can read the repository and knows an object id can
  fetch an object reachable only from a hidden ref (checked with Git 2.54).
  Nothing secret is kept in hidden refs (they hold CI sources, a copy of the
  upstream's default branch, and what merged pull requests merged).
- Limits: `limits.git_http_concurrency` Smart HTTP transfers and
  `limits.git_concurrency` service-side Git commands (separate pools, so a push
  waiting on its hook never blocks the commands that record it), a wall-clock limit per Smart
  HTTP request and per service command, `limits.git_push_max_mib` per push.

## Names and paths

- A name is `[A-Za-z0-9_.-]{1,64}`, not `.`, `..`, or `-`, not ending in `.git`;
  a workspace is a name, a repository path is two names, `<workspace>/<name>`
  (`src/util.rs`) [unit `names_follow_the_contract`].
  Repository directories are derived only from validated paths
  (`Layout::repo_dir`). Branch names are validated before use
  (`git::valid_branch`).
- An upstream is a provider name Branvil knows and a repository name that
  provider validates (GitHub: `owner/name` of letters, digits, `-`, and for the
  name `.` and `_`, never starting with `.`) [unit
  `repository_names_are_owner_and_name`]; the Git URL and every API path are
  built from it, never taken from a request. Every Git command passes the URL
  after `--`.
- A backup is written only to an absolute, empty or new directory outside the
  data directory; a restore refuses manifest paths that are absolute or contain
  `..`, verifies every file's SHA-256 before writing, and fills only an empty
  directory (`src/backup.rs`) [unit `manifest_paths_stay_inside_the_backup`,
  `tests/backup.rs`].
- CI artifact names are `[A-Za-z0-9_.-]` names the job declares, stored as
  `ci/artifacts/<attempt id>/<name>.zip` (attempt ids are checked to be
  `att_<ULID>` before they become file names); uploads must be zip archives
  within the size limit [`tests/ci.rs`]. The runner checks the source out with
  Git (which writes nothing outside the work tree or through a link; it runs
  with an empty global configuration, and never in the work directory once a
  VM has had what it holds), checks that nothing below its work directory is
  a link before a VM gets a directory of it, and hands the job's `/work` (a
  disk of its own) over as archives through a directory the job's VM never
  has. The VM that collects `/work` packs no path through a link; the runner
  unpacks only the artifacts' archive, keeping nothing outside the directory,
  no link it cannot make, and none of the job's modes, collects artifacts
  without following links, and keeps the cache as archives it never unpacks
  [unit `the_pack_script_packs_existing_paths_through_no_link`,
  `returned_archives_unpack_only_inside_their_directory`,
  `artifacts_skip_links_and_stay_inside_the_work_directory`,
  `work_is_collected_after_the_jobs_vm_ended`,
  `cache_round_trip_and_eviction`]. Cache eviction removes superseded generations
  before another repository or job's current entry, then applies LRU within
  each group [unit
  `cache_eviction_removes_superseded_generations_before_other_repositories_and_jobs`,
  `cache_eviction_orders_superseded_entries_by_use_then_key`,
  `cache_eviction_orders_distinct_timestamps_before_keys_and_size`,
  `cache_eviction_breaks_timestamp_ties_by_key_not_size`]. See `docs/ci.md`.

## Markdown

The API stores and returns Markdown as text and never renders HTML. The GUI
renders it in one place (`web/src/components/Markdown.tsx`): `marked`, then
DOMPurify with `style`, `form`, `iframe`, `svg`, `math`, … and the `style`,
`class`, `id` attributes forbidden, and only `http(s):`, `mailto:`, `#`, and
same-site `/…` URLs kept; links open with `rel="noopener noreferrer nofollow"`.
The CSP forbids inline scripts as a second line [`web/src/__tests__/markdown.test.tsx`].
The Markdown's DOMPurify is its own instance, so hooks other libraries add to
the shared one (Mermaid adds some) never reach it. A ` ```mermaid ` block is
drawn by Mermaid (`securityLevel: "strict"`, which sanitizes its labels, in a
temporary element it removes) in `web/src/components/diagram.ts` and shown as
an `<img>` of a `data:` URL, which the CSP's `img-src 'self' data:` already
allows: an SVG loaded as an image runs no script, loads nothing, and keeps its
styles to itself, so the limits above apply unchanged to the rest of the body.
Other consumers of the API (GuildBotics) must treat bodies as untrusted text.

## Secrets at rest

| Secret | Where | Protection |
| --- | --- | --- |
| Data directory | `<data>/` | `0700` (Unix), set at every start [`tests/security.rs` `the_data_directory_is_owner_only`] |
| Member tokens | redb `tokens` | SHA-256 only |
| Local-app codes | memory | SHA-256 only, 60 seconds, single use, bound to the app's name, `redirect_uri`, and PKCE challenge |
| Cursor signing key | memory | per process: a restart ends the list cursors handed out before |
| Hook secret | memory, and the environment of Git children | per process |
| Members' upstream credentials (a token, or a GitHub App's id, installation id, and private key) | the service's vault (see [Upstream tokens](#upstream-tokens)) | sealed; never in the data directory [`tests/upstream.rs` `accounts_are_checked_with_the_host_and_their_tokens_never_returned`] |
| The notification webhook's URL (a credential of the system it posts to, such as Slack) | the service's vault | sealed; never in the data directory, returned (`GET /admin/webhook` shows only its host), or logged (a failure is reported without the URL), not in backups [`tests/autoland.rs` `the_webhook_url_is_a_secret_shown_only_by_its_host`, `tests/backup.rs`] |
| GitHub App installation tokens | memory | made when needed from the app's key, an hour each, made again five minutes before they expire |

On Windows the permission calls are no-ops; the data directory inherits the
user profile's ACL.

## Upstream tokens

- **Whose.** Each member may have one account per upstream host; the owner sets
  it. Writes to the host (the published branch, the pull request) use the
  account of the Branvil pull request's author, so the host shows who did the
  work and a person can approve it there; reads (following the default branch,
  polling, logs, dependency alerts) use the upstream's reader, an agent's account unless no agent
  has one or the owner names another, so the owner's own token is never
  required [`tests/upstream.rs`
  `the_reader_is_picked_once_and_changes_only_when_the_owner_or_a_removal_says_so`].
  A read the host refuses to the reader is reported, never tried again with
  another member's account [`tests/upstream.rs`
  `a_failed_read_of_dependency_alerts_is_never_an_empty_list`].
- **Where.** A credential (a token, or a GitHub App's private key with its ids),
  like the webhook's URL, is kept in the vault that the process running the
  service provides (`src/vault.rs`), never in the data directory. Run by
  GuildBotics (`guildbotics server start` and its commands), that is
  GuildBotics' identity store: AES-256-GCM in
  `<identity>/.guildbotics/local/secrets.enc.json`, with the machine's key in
  the OS keychain, under `branvil_account_<provider>_<hash>` and
  `branvil_webhook_<hash>` (the hash of the data directory's path, and of the
  member; a GuildBotics member's secret is named in capitals, so these are
  never one). When the vault cannot keep or open a value (no key, a locked keychain,
  another key, a damaged file), reading, setting, and removing it are refused
  and every use reports why, the way a missing credential is reported; the
  value stays as it was, and nothing falls back to a plain file
  [`tests/upstream.rs`
  `accounts_are_checked_with_the_host_and_their_tokens_never_returned`,
  `an_app_account_is_checked_with_an_installation_token_and_replaces_a_token_account`,
  `a_vault_that_cannot_keep_or_open_secrets_stops_them_with_the_reason`].
  An app's installation tokens are never written anywhere: they are made from
  the key when needed and kept in memory until shortly before they expire.
- **Never stored elsewhere.** The member record keeps the provider, the kind,
  and the login the host reported, never the credential; no response returns
  it, and a malformed account body is refused without quoting it. The tests
  scan the database file and the service log for the values after a publish
  [`tests/upstream.rs`
  `a_pull_request_is_published_as_its_author_and_follows_the_host_to_the_merge`,
  `an_app_account_reads_and_publishes_with_installation_tokens_renewed_before_they_expire`].
- **How it is used.** The API gets it as a Bearer header over HTTPS. Git gets it
  just before a push or fetch, in the Git child's environment; `GIT_ASKPASS` is
  the `branvil` executable, which answers Git's username/password prompt from
  that environment (`src/upstream/git.rs`). It is never in argv (visible to
  every user in `ps`) or a URL. Process environments are readable by the same
  OS user and root only. Git runs with `credential.helper` cleared,
  `GIT_CONFIG_NOSYSTEM=1`, and an empty global configuration, so no system or
  user credential helper (such as macOS `osxkeychain`) is consulted or written,
  and nothing ever prompts on screen (`GIT_TERMINAL_PROMPT=0`).
- **Errors.** Git's error text is kept (it names the URL, which has no token),
  with the token replaced by `***` if it ever appears, and cut to 600 bytes
  [unit `messages_are_cut_and_never_show_the_token`]; the host's errors keep
  only its status and message.
- **Backups** do not include them; after a restore into another directory,
  polling reports that the credential is not on this machine and nothing is
  sent until it is set again (`docs/backup-restore.md`) [`tests/backup.rs`].
- **CI.** Jobs never receive `secrets/` or these values: the runner gets the
  commit as a Git bundle from the service and nothing else (its checkout keeps
  no remote), and a job's microVM runs its commands as a user that is not
  root, has only its throwaway work and log directories mounted (the runner
  reads the log through a handle it opened before the VM started), no
  network unless the owner allows public destinations, and an environment of CI metadata only (no tokens, no
  host variables; `msb` itself runs with an empty `DOCKER_CONFIG`). The runner's
  own token is a runner member's, which every endpoint outside the runner API
  refuses except `/me` and its own tokens (`docs/ci.md`).

## Upstream behaviour

- Branvil never pushes the default branch of a repository with an upstream,
  and refuses every push to it and every merge there; the branch moves only to
  the host's tip, and only forward. If Branvil's branch has commits the host's
  lacks, following stops and nothing is overwritten [`tests/upstream.rs`
  `a_repository_follows_its_upstream_and_its_default_branch_changes_only_there`].
- A published branch is pushed with `--force-with-lease` naming what Branvil
  pushed before (nothing, the first time), so Branvil only ever replaces its
  own commits; a commit someone added on the host stops sending until the owner
  publishes again [`tests/upstream.rs`
  `a_commit_added_on_the_host_stops_sending_until_published_again`].
- Only the title, the body, and lines for linked issues of the host are
  posted; conversation, reviews, threads, and Branvil URLs stay in Branvil.
  Tickets, pull requests, and boards are **not** backed up by the upstream; use
  `guildbotics server hub-data backup`.
- An unreachable host or a refused token is recorded and retried at the next
  poll; it never holds up pushes, pull requests, reviews, or CI in Branvil.
- Publishing starts the host's own CI for the pull request (for example GitHub
  Actions), as a pull request opened there directly would.
- Real GitHub was **not** used for this build; the tests use a stand-in that
  answers GitHub's REST API (with `ETag` and `304`) and bare repositories as
  its Git side.

## Logging

Logs go to stderr. Branvil does not log request bodies, headers, tokens, or
cookies. Internal errors are logged in detail and answered with a generic
message. Git's stderr from Smart HTTP is logged at `debug`; upstream errors
are logged after the token is removed from them [`tests/upstream.rs` scans the
service log for the tokens].

## What is not protected

- **The same OS user and root** can read everything: the database (all
  tickets, reviews, token hashes), repositories, backups, the
  environment of running Git children, and the vault, as far as the OS
  keychain gives them its key.
- **Sessions and the owner's token are GuildBotics'.** Ending a browser
  session or replacing the owner's token is done in GuildBotics (its owner
  token file under `GUILDBOTICS_HOME/data/server/<port>/`). Revoking a member
  token takes effect at the next request.
- **No rate limit** on failed authentication. Tokens carry 256 bits of
  randomness, so guessing is not practical, but failed attempts are not
  counted or logged.
- **Plain HTTP.** Between machines OpenSSH encrypts the connection; on each machine
  any local process can connect to the forwarded loopback port (it still needs
  a token). The session cookie has no `Secure` flag for the same reason.
- **The owner is trusted**: the owner chooses which repository on the host is
  the upstream and whose tokens publish there, and writes backups anywhere the
  service's user can write.
- **Hidden refs are not secret** from members who can read the repository (see
  Git).
- **Markdown is unsanitized in the API**; only the GUI sanitizes it.
- **Availability.** Limits exist for Git processes, request bodies, and
  timeouts, but a member with write access can still load the machine (many
  large pushes).
- **Backups are not encrypted.** They hold token hashes and all project data;
  store them like the data directory.

## Integrated host origin

GuildBotics and Branvil share one loopback origin (port 7878 by default).
Their cookies and entry links remain separate, but the browser's origin boundary
no longer separates the screens. Both use `loopback_auth::CSP`: scripts and fonts
from self, no objects, no Tauri IPC sources. Rendered user content must remain
sanitized; same-origin scripts can reach either service. Host and Origin checks
still apply on each service's routes. Standalone Branvil continues to use its
own configured origin.
