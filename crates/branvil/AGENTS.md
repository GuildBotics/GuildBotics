# AGENTS.md

A working guide for AI and automation agents in the Branvil crate of the
GuildBotics repository; the repository's own rules are in
[`../../AGENTS.md`](../../AGENTS.md). The structure
is described in [`docs/architecture.md`](docs/architecture.md), and the list of
documents is under "Documents" in [`README.md`](README.md). This file holds
only the rules that reading the code does not reveal and that hurt when broken.

## Most important rules

- The source code is the source of truth (README and `docs/*.md` are reference
  material).
- Do not read files or directories that `.gitignore` excludes, in particular
  `data/` (the development data directory, which holds real tokens such as
  `secrets/owner.token`) and the workspace's `target/`.
- When you change behavior, update the related documents as needed. The HTTP
  API's contract is [`docs/api.md`](docs/api.md), and GuildBotics' `branvil`
  provider relies on it.
- When changing code, give top priority not to the smallest diff but to the
  smallest amount of code after the change. Aim for the clean code the design
  calls for, not an ad hoc patch.
- Do not add code that keeps backward compatibility (migration flags, fallback
  reads, parallel support for old formats, old and new paths side by side
  during a deprecation period). Keeping the code as the design calls for comes
  before keeping redundant code for compatibility. "Important Notice
  (Disclaimer)" in the README declares that incompatible changes happen, and
  in practice the maintainer is the only user.
  - When the stored format changes, raise `SCHEMA_VERSION` in `src/store.rs`
    and replace `src/upgrade.rs` with the upgrade from the previous version
    (`branvil admin upgrade` and `branvil restore` run it). Write no
    conversion from any older version, and read old shapes nowhere else: the
    data directory holds the owner's real data, so a change of the stored
    format must never leave it unreadable.
  - When the HTTP API changes, build no compatibility layer; change GuildBotics'
    `guildbotics/integrations/branvil/` at the same time.
- Judging the meaning of natural language (classifying, deciding whether to
  accept, …) is not Branvil's job. When it is needed, it goes to GuildBotics;
  Branvil does not substitute keyword lists or string matching for it.
- Do not implement across responsibility boundaries (see "Boundaries" below).

## Boundaries

The responsibilities of each module are defined by "Modules" in
[`docs/architecture.md`](docs/architecture.md). Beyond those, these boundaries
hold across modules:

- **Branvil knows nothing of GuildBotics.** The dependency goes one way,
  GuildBotics → Branvil. An embedding application may supply the default runtime
  binary through `set_runtime_binary`; Branvil keeps its own independent runtime
  state. GuildBotics is named only in documents; the
  check that runs GuildBotics' provider against a real service is outside the
  crate (`integration/branvil/`, `scripts/branvil-integration.sh` of the
  repository).
- **The upstream sits behind a provider-neutral boundary.** `src/upstream/`
  knows no code host; everything host-specific goes in the provider's
  implementation of `Host` (`src/upstream/host.rs`), today only
  `src/upstream/github.rs`.
- **Branvil listens on loopback only.** Other machines reach it through OpenSSH
  port forwarding managed by the OS (`src/config.rs` refuses any other listen
  address). SSH connections and their supervision belong outside Branvil.
- **The authenticated data API is the boundary into the data.** The GUI and
  CLI use its HTTP transport. An embedded provider uses `api::DataApi` from
  the same service instance directly; it never opens a separate store. Only
  `branvil init`, `branvil admin check|reindex|upgrade`, and `branvil restore`, run
  with the service stopped, work on the data directory directly.
- **The GUI looks like GuildBotics Desktop.** Keep the Mantine version, the
  theme (`web/src/theme.ts`), and the design tokens and component overrides in
  `web/src/styles.css` the same as GuildBotics Desktop's ("GUI files" in
  `docs/architecture.md`).

## Git and credentials

- Git run by the service goes through `src/git.rs` (it inherits none of the
  service's `GIT_*` and reads no system or global configuration).
- Client Git in tests and scripts must not touch the user's Git configuration
  or credential helpers. Use `git` / `git_as` in `tests/common/mod.rs`; when
  you write the same isolation yourself, set `GIT_CONFIG_NOSYSTEM=1`, an empty
  `GIT_CONFIG_GLOBAL`, `GIT_TERMINAL_PROMPT=0`, and `-c credential.helper=`,
  and pass tokens through `GIT_ASKPASS` and environment variables (never in a
  URL or argv). On macOS the system configuration's
  `credential.helper=osxkeychain` tries to store the token and puts a Keychain
  dialog on the user's screen.
- Do not use real people's names for test members.

## Checks (as in CI)

The crate's checks are the Cargo workspace's and `branvil-web` of the
repository's CI ([`../../.branvil/ci.toml`](../../.branvil/ci.toml); the
commands are in the repository's AGENTS.md, "開発時の基本コマンド（CI 準拠）"):

```sh
cargo fmt --all --check
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
npm ci --prefix ../.. && (cd web && npm run lint && npm run typecheck && npm run test)
```

- The tests run with cargo-nextest, each in a process of its own.
- The integration tests are one test crate (`tests/main.rs`). To run one file:
  `cargo nextest run -p branvil --test main <file>::` (for example
  `cargo nextest run -p branvil --test main tickets::`).
- The tests that use real microVMs (`tests/real_executor.rs`) run only with
  `BRANVIL_REAL_EXECUTOR=1`. They are not part of the usual checks.
- When you change the HTTP API, also run the repository's
  `scripts/branvil-integration.sh` (README.md, "Checking with GuildBotics").
  The pipeline's `branvil-integration` runs it with the same commit.
