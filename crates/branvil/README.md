# Branvil (code host component)

`crates/branvil` is the code host inside GuildBotics, kept under the code name
Branvil: Git repositories over Smart HTTP in workspaces, a task flow for the
tickets of each workspace, pull requests with reviews and per-pull landing
conditions, and a CI that runs each job in a microVM. It keeps metadata in redb
and Git data in bare repositories. Users see it as the task board, pull
requests, task flow, and CI settings of GuildBotics; the user guide is the
[Code Host and CI Guide](../../docs/code_host.en.md).

The GuildBotics server (`guildbotics server start`) prepares it on the
server's listener and creates its data directory
(`GUILDBOTICS_CODE_HOST_DATA_DIR`, by default `GUILDBOTICS_HOME/data/code-host`)
at the first start; the server's owner is its owner, authenticated by
GuildBotics. Its CI jobs run in a separate `guildbotics runner` process, Git
runs its hooks as `guildbotics server git-hook`, and
`guildbotics server hub-data` backs up, restores, checks, upgrades, and
reindexes its data with the rest of the Hub's records. GuildBotics uses its
data API (`api::DataApi`) in process through its agent module, from
`crates/gb-server`. How to run and update the Hub is in the
[macOS Hub guide](../../docs/hub-server.md); see
[Architecture](docs/architecture.md) for the service boundaries.

## Settings

`config.toml` in the data directory holds the code host's settings; every
field is optional and a missing file means defaults (`src/config.rs`). A runner
on another machine reads `<machine root>/runner/config.toml`. The CI sections
are described in [`docs/ci.md`](docs/ci.md).

## Upstream (GitHub)

How the owner sets up and uses an upstream is in the user guide,
[Repositories with an upstream on GitHub](../../docs/code_host.en.md#repositories-with-an-upstream-on-github).

The service polls GitHub with conditional requests (an unchanged answer is a
free `304`): every 60 s while a published pull request is open, every 15 min
otherwise, and at once after publishing or "Check now". GitHub registers the
checks of a pushed head a little later, so a head waits for more checks until
`checks_registration_secs` after the push (or the pull request's creation,
edit, or reopening there) and while a workflow run of it has not finished.
The registration wait defaults to 60 s. The poller also wakes at its deadline
to read GitHub, without waiting for the next regular poll; only a successful
read ends the registration wait. Settings in `config.toml`:

```toml
[upstream]
poll_active_secs = 60
poll_idle_secs = 900
checks_registration_secs = 60
```

Real GitHub was not used for this build; the tests use a stand-in that
answers GitHub's REST API, with bare repositories as its Git side.
Design: [`docs/architecture.md`](docs/architecture.md) ("Upstream"); API:
[`docs/api.md`](docs/api.md) ("Upstream", "Dependency alerts").

## CI runners

How to join a Windows or Linux runner is in the user guide,
[Join another machine as a CI runner](../../docs/code_host.en.md#join-another-machine-as-a-ci-runner).
The runner's internals are in [The runner](docs/ci.md#the-runner).

## Development

The repository's `scripts/dev.sh` builds the GUI and runs the code host, on
data of its own (`code-host/` in the development data directory), inside the
server of the checkout (`guildbotics server start`), and
opens the GUI; see "開発用の起動" in the repository's `AGENTS.md`.

To run the Rust checks:

```sh
cargo fmt --all
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
```

The tests run with [cargo-nextest](https://nexte.st/), each in a process of
its own (`cargo install --locked cargo-nextest`).

To run the GUI checks from the repository root:

```sh
npm ci
npm run quality --workspace desktop
```

Vitest uses at most two workers and writes file and test timings to
`desktop/test-results/timing.json`. The `desktop-quality` CI job uploads this
file as the `web-tests` artifact and streams Vitest output into the job log.
See [Web test measurements](docs/performance.md#web-tests).

The integration tests restart their own test executable as a test server
(`test_support::serve`, with a host of its own that stands in for GuildBotics'
authentication) on free loopback ports and drive it with real Git clients
isolated from the user's Git configuration and credential helpers. They are one test crate,
[`tests/main.rs`](tests/main.rs), with a module per file:
`cargo nextest run -p branvil --test main tickets::` runs the tests of `tests/tickets.rs`.

Branvil browser journeys and fixture layouts run in Desktop E2E:
`cd desktop && npm run e2e -- --project=branvil`. The stack owns a disposable
embedded service and uses the host browser session. Set `GUILDBOTICS_E2E_PERF=1`
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
