# GuildBotics Architecture

**Audience**: Developers and AI agents working on this repository.
**Scope**: System-wide concepts and boundaries. Source code is authoritative; when this
document and the implementation disagree, fix the document (see `AGENTS.md`).

---

## 1. System Overview

GuildBotics runs AI agents as *team members* that collaborate through GitHub and Slack.
A scheduler starts one worker per active member; workers pick up work from GitHub
Projects (tickets) or from incoming Slack events, then delegate the actual
investigation/editing/judgment to an external AI CLI tool (Claude Code, Codex CLI,
Antigravity CLI, ...). External side effects that the AI agent performs *as the member*
— git pushes, GitHub comments and PRs, Slack posts, memory writes — go through a single
boundary: the `guildbotics member ...` CLI (member capability). Workflows additionally
perform a narrow set of orchestration/status writes of their own (see §3).

Users interact with the system through:

- **Desktop app** (`desktop/`, Tauri v2 + React): setup, service runtime control,
  activity history, and diagnostics over a Local API.
- **CLI** (`guildbotics ...`): headless start/run/stop, workspace selection, secrets
  migration, and the member capability entry point.
- **Interactive skill** (`skills/guildbotics/SKILL.md`): lets a user-driven coding agent
  work as a configured member in the user's current repository.

## 2. Package Map and Dependency Rules

```
guildbotics/
├── app_api/         # Desktop-facing Local API (FastAPI + EventBus + normalizers)
├── capabilities/    # Member-side git/github/chat/memory operations + domain events
├── cli/             # Click commands (start/run/stop, workspace, secrets, member)
├── commands/        # Command framework (md/py/sh/yml + inline commands)
├── drivers/         # Scheduler, command runner, workflow dispatcher, event listeners
├── editions/        # Edition abstraction + Simple edition (setup services reused by GUI)
├── entities/        # Domain models (Team, Person, Task, Message)
├── integrations/    # GitHub / Slack clients (used by capabilities and workflows)
├── intelligences/   # Brains (agno_agent / cli_agent), LLM judgment functions, catalogs
├── loader/          # YAML team/role loaders
├── observability/   # Diagnostics records, trace/span correlation, interactive sessions
├── runtime/         # Context, member resolution, factories, WorkflowInvocation
├── templates/       # Config templates, workflow commands, prompts, locales
└── utils/           # fileio (config/storage roots), secret store, i18n, ...
```

Hard dependency rules (enforced by `tests/guildbotics/test_layer_boundaries.py`):

- `app_api` is the top layer: no other guildbotics package may import it. Knowledge
  needed by both app_api and core (e.g. LLM provider / AI CLI tool catalogs) lives in
  core (`guildbotics/intelligences/*`); app_api only converts it to API models.
- `observability` depends on nothing but `utils`. It records; it does not know about
  app_api or capability concerns.
- Lower layers (`entities`, `utils`) never depend on orchestration layers
  (`commands`, `templates`, `drivers`).

Responsibility boundaries between CLI / capabilities / observability / app_api /
desktop frontend — who may know provider payloads, who normalizes display titles, etc. —
are specified in `AGENTS.md` (「重要な実装ポイント > 責務境界」). This document covers the
concepts; `AGENTS.md` is the working rulebook.

## 3. Member Capability Delegation Model

The central design of GuildBotics is a three-party delegation chain, identical for the
ticket workflow, the chat workflow, and interactive skill sessions:

```
workflow (orchestration)
  -> AI CLI agent (judgment)
     -> guildbotics member ... CLI (external side-effect boundary)
```

- **Workflow** (`guildbotics/templates/commands/workflows/*`): selects the work item,
  builds the prompt payload, launches the CLI agent with the member workspace as cwd,
  and afterwards verifies run completion. It never produces work products
  (code changes, PRs, replies, review comments) itself, but it does perform a narrow
  set of orchestration/status writes of its own through its integration clients:
  moving the Project lane, posting rate-limit / failure status comments on the ticket,
  and posting failure notices in the Slack thread.
- **CLI agent**: reads the ticket/thread, investigates, edits, and decides. It never
  receives GitHub/Slack tokens and never calls provider APIs directly; every external
  write goes through `guildbotics member ...`.
- **Member capability** (`guildbotics/capabilities/*` + `guildbotics/cli/member.py`):
  the boundary for member actions decided by the AI agent — the only path through
  which the agent's provider operations run and the only layer that resolves the
  member's credentials for them. It records run evidence (`task_runs.py`) for each
  side effect.

Trust rules that follow from this shape:

- The workflow does not trust the agent's natural-language stdout as the result. Only
  the completion record (`member task complete` / `member chat complete`) and the run
  evidence store are authoritative.
- Credentials are resolved by the member CLI itself from the active workspace (config +
  secret store), never inherited from the agent's environment — AI CLI tools strip or
  isolate `*TOKEN*`-like variables from subprocess environments, so inheritance cannot
  be relied on anyway.
- The same capability boundary serves both non-interactive runs (scheduler-spawned
  agents) and interactive runs (a user's coding agent using the GuildBotics skill), so
  "acting as the member" means the same thing in both modes.

Member-facing agent instructions are layered so each contract has exactly one source
(shared reference → workflow envelope → trigger-specific prompt → interactive SKILL).
The layer model and its guard tests are described in `AGENTS.md`
(「member プロンプト層モデル」).

## 4. Workflow Invocation

Workflow start-up is normalized by `guildbotics/runtime/workflow_invocation.py`:

```python
WorkflowInvocation(command, person_id, source, trigger_type, payload, idempotency_key)
# source:       routine | scheduled | event_queue | manual
# trigger_type: ticket | chat | scheduled | generic
```

`drivers/workflow_dispatcher.py` runs every invocation the same way (trace scope,
attributes, `Context.shared_state` injection), while discovery stays asymmetric on
purpose:

- **Ticket workflow** (`workflows/ticket_driven_workflow`, the default routine of the
  Simple edition): a routine that *polls* GitHub ProjectV2. The project board is treated
  as a loose queue/trigger — GuildBotics does not reimplement fine-grained GitHub/git
  operations around it, and there is no GitHub webhook receiver (local-first design).
  Ticket selection lives in `drivers/ticket_selector.py`.
- **Chat workflow** (`workflows/chat_conversation_workflow`): Slack Socket Mode events
  and backfill are persisted as pending events by `drivers/event_listener_runner.py`,
  then drained per member by `drivers/pending_chat_dispatcher.py`.

Invariants:

- Scheduler-managed work is serial per person: one worker thread per active member
  (`drivers/task_scheduler.py`) runs scheduled, routine, and queued-event work one at
  a time, so those runs never use the member's workspace concurrently. This guarantee
  does not extend to executions started outside the scheduler — App API manual
  commands, `guildbotics run`, or interactive sessions — which are tracked by
  `drivers/execution.py` (`ExecutionCoordinator`) but not serialized per person and
  not excluded across processes.
- The long-running background service is singleton per machine. CLI `guildbotics
  start` and the Desktop-managed service contend on the same OS advisory lock at
  `<machine-state-root>/run/service.lock`; the lock covers scheduler workers and the
  event listener together. The persistent file contains owner metadata for status and
  CLI stop handling, but file existence is not a liveness signal. Desktop-owned
  services must be stopped from Desktop rather than by signalling the sidecar PID.
- Rate limits from AI CLI tools are detected (`intelligences/brains/cli_agent.py`),
  handled by shared capability logic (`capabilities/workflow_rate_limits.py`,
  `capabilities/completion_retry.py`), surfaced as a `workflow.rate_limited`
  diagnostics event, and never amplified by retries — the run stops with a visible
  notice on the ticket/thread instead.

## 5. Command Execution Framework

The generic execution substrate used by workflows and custom commands:

- `drivers/command_runner.py` resolves the target member, builds a `CommandSpec`
  (`commands/models.py`, via `commands/spec_factory.py`), runs child commands
  (`commands:`) first, then the main command.
- Command types (`commands/registry.py`): `.md` (LLM prompt), `.py`, `.sh`,
  `.yml`/`.yaml` (definition), plus inline commands `print`, `to_html`, `to_pdf`.
- `Context.pipe` carries stdin/stdout-like text between commands;
  `Context.shared_state` carries structured state. Their update order is workflow
  compatibility surface — change with care.
- Config file resolution (`utils/fileio.py`): primary config
  (`GUILDBOTICS_CONFIG_DIR` or cwd `.guildbotics/config`) → package templates.
  Localized files resolve `.<lang>` → `.en` → bare name; person-specific commands
  (`team/members/<person_id>/...`) take precedence over shared ones.

See `docs/custom_command_guide.en.md` / `.ja.md` for the user-facing guide.

## 6. Member Memory

Members persist knowledge across runs as a document store (mechanism in
`capabilities/member_memory.py`; audit in `capabilities/member_memory_audit.py`).

- **Document**: 1 document = 1 directory (`meta.yml` + `body.md` + `assets/`) under
  `<workspace-data-root>/documents/`. `meta.yml` holds title/summary/keywords, typed
  `source` entries (ticket/PR/channel/thread URLs), creation/update timestamps and
  member IDs (`created_at`/`created_by`, `updated_at`/`updated_by`), `pinned`, and
  `kind`.
- **Scopes**: `personal/<person_id>/` and `team/`. `memory promote` moves a document
  from personal to team.
- **Operations** (`guildbotics member memory ...`): `record`, `recall` (lexical grep
  over meta+body; `--meta-only` for source-URL pinpointing), `get`, `update`,
  `touch` ("this note actually helped" — recency only), `archive`, `promote`.
- **Recency (MRU)**: `recent.txt` per member; `record`/`update`/`touch` move a doc-id
  to the top, read operations do not. `member context` output includes a `memory`
  block: `digest` (top-N recent metas — hints that a relevant note exists) and
  `pinned` (always-on documents, body included).
- **Mechanism vs policy**: the code fixes only the mechanism above. What to record and
  how to classify it is team-owned policy, kept as a pinned team document
  (`kind: policy`) that agents may not change autonomously (human-approved updates
  only).
- **Hot-path upkeep**: every run recalls at the start (source URL first, then keyword
  OR-search) and tends memory at the end (touch what helped, update what was wrong,
  record new learnings). Cold-path maintenance of never-recalled documents is a
  planned follow-up
  ([GuildBotics/GuildBotics#272](https://github.com/GuildBotics/GuildBotics/issues/272),
  unimplemented).

## 7. Storage Roots

Path resolution separates four roots (implementation: `utils/fileio.py`).

| Root | Location | Holds |
|---|---|---|
| Machine state root | `$HOME/.guildbotics/data` (fixed) | `active-workspace.json`, `run/service.lock` — state needed *before* a workspace is chosen |
| Runtime workspace root | selected workspace (App API `chdir`s to it; member CLI resolves `--workspace` → explicit config → cwd → active workspace) | `.env`, `.guildbotics/config` |
| Workspace data root | `<workspace>/.guildbotics/data`, overridable via `GUILDBOTICS_DATA_DIR` | member workspaces (`workspaces/<person_id>`), task-run evidence (`task-runs/*.jsonl`), diagnostics (`run/diagnostics.jsonl`), prompt trace, chat state, documents |
| Config root | `GUILDBOTICS_CONFIG_DIR` or cwd `.guildbotics/config`; package templates as fallback | project / member configuration |

Invariants:

- Machine state root is always derived from `HOME` and never affected by
  `GUILDBOTICS_DATA_DIR`, `GUILDBOTICS_CONFIG_DIR`, or workspace `.env`.
- `GUILDBOTICS_CONFIG_DIR` selects the *config source* only; it is not a workspace or
  data root.
- The effective workspace data root is fixed at the *workspace application boundary*
  (App API `set_workspace()`, CLI/member CLI startup, `run`/`start` initialization) and
  written to `os.environ["GUILDBOTICS_DATA_DIR"]` there — and only there. Workers,
  workflows, and commands never mutate it mid-run; per-invocation values travel via
  subprocess env overlays (e.g. `cli_agent_env`).
- Workflows must pass `GUILDBOTICS_DATA_DIR` (and the run id) to AI CLI tool
  subprocesses: the agent's cwd is a member workspace, so without the explicit value a
  child process would compute a wrong, nested data root.
- Stores that keep a path (diagnostics store, chat state store) must re-resolve or be
  rebound on workspace switch; App API keeps the process-startup
  `GUILDBOTICS_DATA_DIR` as the inherited fallback so switching workspaces never leaks
  the previous workspace's root.

## 8. Secret Storage (SecretStore)

Secrets = LLM provider API keys (`models/<provider>/default.yml` `api_key_env`) and
person secrets (`GITHUB_ACCESS_TOKEN` / `GITHUB_PRIVATE_KEY` / `SLACK_BOT_TOKEN` /
`SLACK_APP_TOKEN`). Non-secret IDs/paths stay in `.env`.

- **Backends** (`utils/secret_store.py`): `keyring` (OS keychain; the workspace keeps
  only a value-less index in `.guildbotics/config/secrets.yml`) and `env-file`
  (workspace `.env`; the default for legacy workspaces and the headless path). New
  workspace setup defaults to keyring; existing workspaces switch only via
  `guildbotics secrets migrate`. Backend selection: `GUILDBOTICS_SECRETS_BACKEND` env
  var > `secrets.yml` > legacy env-file.
- **Resolution priority** at process start: real environment variables > OS keychain >
  `.env`. Values are injected into `os.environ` once at startup; app_api removes its
  own injected keys on workspace switch but preserves variables inherited from the
  parent process.
- **Exception**: `*_GITHUB_PRIVATE_KEY` (GitHub App PEM content) is *never* injected
  into the environment — AI CLI tool subprocesses inherit `os.environ` wholesale, so an
  App private key in the environment would leak to every agent process. Consumers read
  it on demand through the secret store, falling back to the legacy
  `*_GITHUB_PRIVATE_KEY_PATH` file.
- **Writes**: `.env` files written by GuildBotics are always `0600`, written atomically
  (`mkstemp` + `os.replace`); dotenv serialization round-trips multiline values (PEM).
  Workspace moves use `guildbotics secrets export` / `import`.
- **Tests**: an autouse fixture forces `GUILDBOTICS_SECRETS_BACKEND=env-file` so tests
  never touch a real keychain; keyring paths are tested with the `fake_keyring`
  fixture.

## 9. Observability and Diagnostics

`guildbotics/observability/` is the recording substrate (depends only on `utils`):

- **Correlation**: `trace_scope` / `span_scope` / `correlation_fields` /
  `set_attributes` correlate everything that happens in one execution — a manual
  command, one scheduler cycle, one incoming chat event — under a single `trace_id`,
  with spans for LLM / AI CLI tool calls (shared with prompt trace via `call_id`).
  Trace attributes carry structured search keys (e.g. `github.url`, `github.number`).
- **Persistence**: unified diagnostics records in
  `<workspace-data-root>/run/diagnostics.jsonl` (`diagnostics_store.py`), with size-
  and count-based retention. Prompt traces go to `run/prompt_trace.jsonl`
  (`utils/prompt_trace.py`). Correlated events are recorded via
  `diagnostics_events.py`; interactive skill sessions via `interactive_sessions.py`.
- **Consumption**: `app_api` reads the stores, merges traces / logs / events / memory
  audit / prompt traces, and converts provider payloads into provider-neutral activity
  events/links/titles for the desktop Activity History (`activity_events.py`,
  `activity_links.py`). Display normalization lives *only* there — not in
  observability, not in the frontend.
- **System health alerts**: successful and failed verify/scenario-diagnostics runs are
  recorded as structured diagnostics events. `app_api/system_alerts.py` folds those
  events together with command outcomes, rate-limit events, and current runtime state
  into deduplicated unresolved alerts. The materialized state is retained separately
  in `<workspace-data-root>/run/system-alerts.json`, so diagnostics rotation does not
  discard an unresolved condition. A durable JSONL cursor advances across newly
  appended records and folds only alert-relevant events; processed-record hashes are
  not retained, so logs and spans do not grow the state file. Credential checks and
  runtime failures open on the first failure; a matching successful check/execution or
  removal of the affected member/provider closes the alert. A user may also dismiss the
  current occurrence; a later occurrence of the same cause opens it again. Provider adapters
  normalize structured authentication failures to `credential.failed`, allowing a
  workflow-time failure to become critical without waiting for diagnostics. The desktop
  polls `/system-alerts` and renders the result above every route with links to
  diagnostics, setup, service state, or the correlated trace. Provider classification
  remains in the backend; the frontend only translates stable alert codes.

## 10. Desktop App

`desktop/` is a Tauri v2 + React (Mantine, TanStack Query) frontend; the repository is
a monorepo on purpose.

- **Local API boundary**: the GUI never reimplements the Python engine. It launches the
  bundled backend (`python -m guildbotics.app_api`, FastAPI) and talks to it on
  `127.0.0.1` via REST + WebSocket. Every request requires the per-process
  `X-GuildBotics-Session-Token`; `/health` is the liveness probe.
- **Runtime lifecycle**: the sidecar manages member workers / event listeners inside
  its own process and reports states to the UI. Desktop and CLI `guildbotics start`
  use the same machine-wide `service.lock`, so only one background service can own
  scheduler workers / event listeners at a time. CLI `start` remains the headless
  equivalent.
- **Packaging**: `scripts/desktop-build-backend.sh` builds two PyInstaller sidecars
  (`guildbotics-app-api`, `guildbotics-cli`) into `desktop/src-tauri/binaries/`. The
  app also installs a managed `guildbotics` CLI shim and the GuildBotics skill for
  interactive agents. External AI CLI tools are *not* bundled — the GUI detects,
  verifies, and configures them only.
- **Support targets**: macOS Apple Silicon (DMG) and Linux x86_64 (`.deb` / AppImage).
  The CLI remains the fallback everywhere else.

See `desktop/README.md` for build, development, and test instructions.

## 11. Domain Model Notes

Core entities live in `guildbotics/entities/` (`Team`, `Person`, `Task`, `Message`).
Two Person distinctions matter architecturally:

- **AI members** are execution subjects: active members get a scheduler worker, Slack
  subscriptions, credentials, and an intelligence (brain) configuration.
- **Human members** are *references to real people* (roles, Slack user id, GitHub
  username, avatar) used for handoff candidates and assignee mapping. They are saved
  with `is_active: false`, are never scheduled, hold no bot/app credentials, and the
  desktop settings UI intentionally hides all agent-execution fields for them. Treat
  any path that would execute a human member as a boundary bug, not a feature.

## 12. Extension Points

- **New brain**: implement `Brain`, register via the intelligence mappings
  (`guildbotics/templates/intelligences/*.yml`). AI CLI tool definitions are cataloged
  in `intelligences/cli_agents.py` + `templates/intelligences/cli_agents/*.yml`.
- **New command type**: subclass `CommandBase` with `extensions` / `inline_key`; the
  registry picks it up (`commands/registry.py`).
- **New integration**: implement `TicketManager` / `ChatService` and wire it in the
  edition's `SimpleIntegrationFactory`.
- **New edition**: implement `Edition` (`get_context()` / `get_default_routines()`)
  and select it via `GUILDBOTICS_EDITION`.
- **New activity event/link kind**: add the recording payload (capability →
  observability), the normalizer/API model (app_api), and the frontend rendering
  (desktop) as three separate responsibilities.

## 13. Related Documents

- `AGENTS.md` — working rules for this repository: responsibility boundaries, CI
  commands, testing strategy, prompt layer model.
- `README.md` / `README.ja.md` — user-facing setup and usage.
- `docs/custom_command_guide.en.md` / `.ja.md` — custom command development guide.
- `desktop/README.md` — desktop build/dev/test guide.
