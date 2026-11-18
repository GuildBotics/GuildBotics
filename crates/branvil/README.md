# Branvil

Branvil is the code hosting of GuildBotics: Git repositories over Smart HTTP
in workspaces, each workspace with one kanban board for the tickets of its
repositories, pull requests with reviews and per-pull landing conditions, and
a CI that runs each job in a microVM. It keeps metadata in redb and Git data
in bare repositories.

It is an internal component, not a product of its own. The GuildBotics server
(`guildbotics server start`) prepares it on the server's listener and creates
its data directory (`BRANVIL_DATA_DIR`, by default
`GUILDBOTICS_HOME/data/code-host`) at the first start; the server's owner is
its owner, authenticated by GuildBotics. Its CI jobs run in a separate
`guildbotics runner` process, Git runs its hooks as
`guildbotics server git-hook`, and `guildbotics server hub-data` backs up,
restores, checks, upgrades, and reindexes its data with the rest of the Hub's
records. GuildBotics uses its data API (`api::DataApi`) in process through
its agent module, from `crates/gb-server`. How to run and update the Hub is in the
[macOS Hub guide](../../docs/hub-server.md); see
[Architecture](docs/architecture.md) for the service boundaries.

## Important Notice (Disclaimer)

- This software is in alpha. Its data format, HTTP API, and configuration
  will change incompatibly without notice.
- The author and distributor assume no responsibility for malfunctions of this
  software or damages caused thereby. Use it at your own risk.

## Upstream (GitHub)

Branvil is where agents try things — many pushes, review rounds, and CI runs,
locally and without cost. A repository can have an **upstream** on GitHub that
stays the place of record: its protected default branch, people's approvals
and merges, its CI (Windows included), releases, and the pull requests people
outside see. Branvil never pushes the default branch; it follows it.

1. Give every agent a GitHub account in Branvil, checked with GitHub: the
   account that publishes its pull requests. It is a GitHub App (its app ID,
   the ID of its installation on the account that owns the repositories, and
   its private key; it acts as `<app>[bot]`, and Branvil makes its short-lived
   tokens itself) or a token of a bot account. One of them also reads GitHub
   for Branvil, so your own token is not needed (set it only to publish pull
   requests you author yourself). Set them under Settings → Members → a
   member → Accounts on code hosts.

2. Point the repository at its GitHub repository in its settings (Upstream).
   An empty repository is filled with GitHub's default branch; an existing one
   must be the same or behind. The settings show whose account reads it and
   choose another.

3. Agents work in Branvil as before. On a pull request the owner finds
   **Publish to GitHub** where the merge button was: it shows exactly what will
   be posted (title, body, and `Closes #12` for linked tickets that name a
   GitHub issue), then pushes the branch and opens the pull request on GitHub
   as the author's account. GitHub's checks of the head appear among the pull
   request's checks (`github/<name>`, with their logs), later pushes and edits
   follow, and once a person merges on GitHub, Branvil's default branch follows
   and its pull request and tickets close.

   With **Land automatically** on, Branvil publishes a pull request by itself
   once it meets the conditions. Once the published head's host checks succeed
   and no more can arrive, Branvil tells the webhook once per PR head, with
   links to both pull requests. Failed host checks are reported in a comment.
   Under
   Settings → Notifications, the owner enters its URL (a Slack Incoming Webhook takes the
   message as it is), which is kept like a credential and never shown again.
   Creating a repair ticket after main verification fails sends one notice with
   the failed jobs, commit range, ticket URL and assignee to this webhook.

Members read the GitHub repository's Dependabot alerts through Branvil
(`docs/api.md`, "Dependency alerts"), with the reader's account; Branvil
keeps none of them. For that the reader needs Dependabot alerts enabled for
the repository and, for a GitHub App, Repository permissions → **Dependabot
alerts: Read-only** with access to the repository (adding a permission to an
installed app takes effect once the account that installed it approves it);
for a token, the same permission (fine-grained) or the `security_events`
scope (classic).

Branvil polls GitHub with conditional requests (an unchanged answer is a free
`304`): every 60 s while a published pull request is open, every 15 min
otherwise, and at once after publishing or "Check now". GitHub registers the
checks of a pushed head a little later, so a head waits for more checks until
`checks_registration_secs` after the push (or the pull request's creation,
edit, or reopening there) and while a workflow run of it has not finished.
The registration wait defaults to 60 s. The poller also wakes at its deadline
to read GitHub, without waiting for the next regular poll; only a successful
read ends the registration wait.
The reading account needs, for a GitHub App, Repository permissions →
**Checks: Read-only** and **Actions: Read-only** (the workflow runs and the
jobs' logs); for a token, the same permissions (fine-grained) or the `repo`
scope (classic).
Settings in `branvil.toml`:

```toml
[upstream]
poll_active_secs = 60
poll_idle_secs = 900
checks_registration_secs = 60
```

Notes:

- Conversation, reviews, threads, tickets, and boards stay in Branvil; back
  them up with `guildbotics server hub-data backup`.
- A ticket names the GitHub issue it stands for in its "GitHub issue" field
  (`upstream_issue` in the API); without it nothing about the ticket is written
  on GitHub.
- Branvil's references in the title and body (`#3`, `acme/demo#3`, `!3`, its
  URLs) mean something else on GitHub, so publishing rewrites them: to the
  GitHub issue or pull request they stand for, or to a code span that GitHub
  does not link. The preview shows the result; when it still needs a hand, the
  owner edits the title and body in the publish panel.
- Real GitHub was not used for this build; the tests use a stand-in that
  answers GitHub's REST API, with bare repositories as its Git side.
- Design: [`docs/architecture.md`](docs/architecture.md) ("Upstream"); API:
  [`docs/api.md`](docs/api.md) ("Upstream").

## Development

The repository's `scripts/dev.sh` builds the GUI and runs Branvil, on data of
its own, inside the server of the checkout (`guildbotics server start`), and
opens the GUI; see "開発用の起動" in the repository's `AGENTS.md`.

To run the Rust checks:

```sh
cargo fmt --all
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
```

The tests run with [cargo-nextest](https://nexte.st/), each in a process of
its own (`cargo install --locked cargo-nextest`).

To run the GUI checks (lint, types, and the Vitest tests):

```sh
npm ci --prefix ../..
cd web
npm run lint
npm run typecheck
npm run test
```

The tests use at most four workers and write file and test timings to
`web/test-results/timing.json`; CI keeps this in the `web-tests` artifact
and streams Vitest output into the job log. Measurements and interpretation
are in
[`docs/performance.md`](docs/performance.md#web-tests).

The integration tests restart their own test executable as a test server
(`test_support::serve`, with a host of its own that stands in for GuildBotics'
authentication) on free loopback ports and drive it with real Git clients
isolated from the user's Git configuration and credential helpers. They are one test crate,
[`tests/main.rs`](tests/main.rs), with a module per file:
`cargo nextest run -p branvil --test main tickets::` runs the tests of `tests/tickets.rs`.

Branvil browser journeys and fixture layouts run in Desktop E2E:
`cd desktop && npm run e2e -- --project=branvil`. The stack owns a disposable
embedded service and uses the host browser session. Set `BRANVIL_E2E_PERF=1`
only for one-card fixture timing investigations; these are not large-board benchmarks.

### GitHub Actions

The GuildBotics repository's
[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) runs only what its
pipeline cannot. Its `clippy-macos` job runs
`cargo clippy --locked --workspace --all-targets -- -D warnings` on
`macos-latest` with the Rust of `rust-toolchain.toml`, which compiles and lints
the code built for macOS alone, when `*.rs`, a `Cargo.toml`, `Cargo.lock`, or
`rust-toolchain.toml` changes (a change to the workflow runs it too). The job
reports for every change, with the steps it does not concern skipped. The
opt-in real microVM executor tests still require the runtime and image
described in [`docs/ci.md`](docs/ci.md).

For a pull request published to GitHub by the owner, Branvil imports the job's
result as `github/clippy-macos` (see "Upstream (GitHub)").

### Checking with GuildBotics

From the repository root, run the integration check against a disposable
Branvil service:

```sh
cargo nextest run --locked -p branvil --features test-support -E 'test(/^agent/)'
```

The check covers tickets and dependencies, Git push and the pull request that
closes a ticket, and CI readiness when a job completes or the head moves. It
uses the agent module and the public Branvil data API. It needs no existing
service, real credentials, browser, or microVM. The same check runs in CI.

## Documents

- [`docs/api.md`](docs/api.md): the internal HTTP API of the screens, the runner, and the Git hooks
- [`docs/port-mapping.md`](docs/port-mapping.md): GuildBotics port operations → API
- [`docs/storage-design.md`](docs/storage-design.md): tables, keys, indexes, transactions
- [`docs/architecture.md`](docs/architecture.md): modules, ref-change coordination, recovery, CI, upstream, backups
- [`docs/ci.md`](docs/ci.md): pipelines, trust model, states, limits, runner, executor and its prerequisites
- [`docs/performance.md`](docs/performance.md): measurements (taken with a load tool of the standalone service, since removed)
- [`docs/security.md`](docs/security.md): threat model, authentication, Git, secrets, upstream tokens, what is not protected
- [`docs/backup-restore.md`](docs/backup-restore.md): backup contents, consistency, restore, credentials
