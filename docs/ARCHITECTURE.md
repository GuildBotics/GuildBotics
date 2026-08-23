# GuildBotics Architecture

**Audience**: Developers and AI agents working on this repository.
**Scope**: System-wide concepts and boundaries. Source code is authoritative; when this
document and the implementation disagree, fix the document (see `AGENTS.md`).

---

## 1. System Overview

GuildBotics runs AI agents as _team members_ that collaborate through GitHub and Slack.
A scheduler starts one worker per active member; workers pick up work from GitHub
Projects (tickets) or from incoming Slack events, then delegate the actual
investigation/editing/judgment to an external AI CLI tool (Claude Code, Codex CLI,
Antigravity CLI, ...). External side effects that the AI agent performs _as the member_
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
├── hub/             # The bare repositories a hub holds, and reaching one over OpenSSH
├── integrations/    # GitHub / Slack clients (used by capabilities and workflows)
├── intelligences/   # Brains (agno_agent / cli_agent), LLM judgment functions, catalogs
├── loader/          # YAML team/role loaders
├── observability/   # Diagnostics records, trace/span correlation, interactive sessions
├── runtime/         # Context, member resolution, factories, WorkflowInvocation
├── sync/            # The Git sync queue: local repository, commits, enrollment, activation
├── templates/       # Config templates, workflow commands, prompts, locales
├── workspace/       # Workspace storage: identity, shared-file validation, config CAS
└── utils/           # fileio (config/storage roots), secret store, i18n, ...
```

Hard dependency rules (enforced by `tests/guildbotics/test_layer_boundaries.py`):

- `app_api` is the top layer: no other guildbotics package may import it. Knowledge
  needed by both app_api and core (e.g. LLM provider / AI CLI tool catalogs) lives in
  core (`guildbotics/intelligences/*`); app_api only converts it to API models.
- `observability` depends on nothing but `utils`. It records; it does not know about
  app_api or capability concerns.
- `workspace` depends on nothing but `utils` and `entities`. It is storage; it does not
  know about capabilities, drivers, or app_api.
- `hub` depends on nothing but `utils`. It knows about repositories and an SSH route,
  never about what the shared records mean.
- `sync` may be imported **only by a composition root**, listed in
  `tests/guildbotics/test_layer_boundaries.py` (`SYNC_COMPOSITION_ROOTS`). Everything
  else reaches synchronization through the Workspace Sync Port. The Desktop backend
  and `guildbotics start` install a queue; member CLI uses the same boundary for one
  locked commit/push. The process-local activation guard is supplemented by
  `local/run/sync.lock`, so two processes cannot operate the same repository at once.
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
     -> trusted member transport
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
- **Trusted member transport**: interactive agents invoke the member CLI directly.
  A sandboxed Grok session instead receives one authenticated localhost MCP tool that
  can invoke only the fixed member CLI for the active person, workspace, and turn. The
  broker, not Grok, holds the short-lived execution delegation and a separately
  rotated per-turn grant, and terminates any outstanding member command when the turn
  ends. The broker keeps the selected GuildBotics workspace root (used by the member
  CLI for config resolution) separate from the agent's isolated working directory and
  the independently overridable workspace data root.
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
  Simple edition): a routine that _polls_ GitHub ProjectV2. The project board is treated
  as a loose queue/trigger — GuildBotics does not reimplement fine-grained GitHub/git
  operations around it, and there is no GitHub webhook receiver (local-first design).
  Ticket selection lives in `drivers/ticket_selector.py`.
- **Chat workflow** (`workflows/chat_conversation_workflow`): Slack Socket Mode events
  and backfill are persisted as pending events by `drivers/event_listener_runner.py`,
  then drained per member by `drivers/pending_chat_dispatcher.py`.

Invariants:

- Scheduler-managed work is serial per person: one worker thread per active member
  (`drivers/task_scheduler.py`) runs scheduled, routine, and queued-event work one at
  a time. `runtime/person_lease.py` extends that guarantee to App API manual commands,
  `guildbotics run`, interactive sessions, and separate GuildBotics processes with one
  OS advisory lease per person. Nested `member` writes require the exact delegated
  lease/run identity; environment-variable presence alone never authorizes them.
  Read-only `member` commands declare that capability on their Click callback;
  undeclared commands fail closed as write-capable instead of being classified from
  command-name strings.
- The long-running background service is singleton per machine. CLI `guildbotics
start` and the Desktop-managed service contend on the same OS advisory lock at
  `<machine-state-root>/run/service.lock`; the lock covers scheduler workers and the
  event listener together. The persistent file contains owner metadata for status and
  CLI stop handling, but file existence is not a liveness signal. Desktop-owned
  services must be stopped from Desktop rather than by signalling the sidecar PID.
- Chat events are drained FIFO per Slack thread (`drivers/pending_chat_dispatcher.py`):
  while a thread's oldest event is retrying or backing off, later events of the same
  thread are not dispatched (a newer arrival instead wakes the waiting head early,
  at most once per follower cursor and never before a provider rate-limit reset), and
  a finally-abandoned event is terminalized so it can never block its thread. This
  keeps the shared provider conversation's context cursor a monotonic watermark.
  A resumed session is continued only for the same run/event it last worked on;
  cursor regression, unorderable cursors, or a run/event identity mismatch rotate the
  session, record a `continuation_rejected` diagnostics event, and re-feed full
  context instead of sending the generic continuation prompt.
- Rate limits from AI CLI tools are detected (`intelligences/brains/cli_agent.py`),
  handled by shared capability logic (`capabilities/workflow_rate_limits.py`,
  `capabilities/completion_retry.py`), surfaced as a `workflow.rate_limited`
  diagnostics event, and never amplified by in-process retries. Ticket selection and
  the chat pending queue defer re-entry until the provider's exact reset timestamp
  when one is available.
- Diagnostics keep provider turn success, workflow completion evidence, and dispatch
  lifecycle as separate layers (`capabilities/workflow_completion_events.py`): a
  clean provider turn without a recorded completion raises
  `workflow.completion_missing`, the dispatcher records
  `chat_dispatch.retry_scheduled` / `chat_dispatch.abandoned`, and status
  derivation (execution index summaries and activity history sessions) never
  reports workflow success from `span.finished` alone when completion-layer events
  exist. `retry_scheduled` requires an actual `chat_dispatch.retry_scheduled` event;
  completion evidence alone does not imply a retry is scheduled, since the ticket
  workflow shares the same completion layer but exhausts its attempt budget by
  posting an error comment instead of dispatching a retry, so missing completion
  evidence without a dispatch event resolves to `incomplete`. Traces without
  completion gating (manual/interactive) keep the span-derived status.

### Native agent runtime

`intelligences/agent_runtime/*` is the provider-neutral boundary for Codex App Server
and Claude Code stream-json. It defines execution context, conversation key/record,
normalized events, terminal results, errors, explicit resume policy, process lifecycle,
and redacted diagnostics. Provider adapters own only protocol translation.

Logical conversations are keyed by person, adapter, work kind, and stable work identity
(ticket URL or Slack bot/channel/thread root), not by scheduler run. The versioned
atomic store under `agent-runtime/conversations/` persists the provider-neutral
conversation identity plus provider session/turn ids, context cursor, usage, health,
and rotation metadata. A provider process is an
execution resource; its session identity survives process restart. Exact resume never
falls back to a provider's latest session. See
`docs/native_agent_runtime.en.md` / `.ja.md` for configuration and operations.

Chat context delivery is session-aware at the brain boundary. The workflow supplies
the latest event separately from a bounded thread snapshot. A healthy session
receives only the new event; a new or rotated session receives the earlier snapshot
once.

### Model effort

Effort is the single provider-neutral vocabulary for how hard a model should
think: `low` / `default` / `high`. `guildbotics/intelligences/effort.py` owns the
labels, their ordering, and the resolution order every brain shares — the
runtime channel (`session_state["effort"]`, carrying both a CLI `effort=<level>`
parameter and a workflow's automatic assessment) beats the command frontmatter,
which beats nothing at all. Because both sources arrive on one channel, a
workflow's decision keeps working when a brain slot is switched between the LLM
API and an AI CLI tool.

Translating a label into concrete settings belongs strictly to the layers that
hold provider knowledge: the definition YAML of a model or of an AI CLI tool,
and the native adapters, which each allowlist the keys they can apply. The core
understands only the common `model` key, which feeds the settings fingerprint;
every other key belongs to the adapter. A level with no mapping is a
warning, never a failure — mapping coverage differs per provider, and halting a
run over a missing overlay would cost more than running without it.

Both kinds of definition have the same two layers. `parameters:` always applies,
whatever effort was asked for, and `effort.<level>` overlays it. `default` is
rejected as a mapping key, because a mapping for "do not intervene" could never
be applied; settings that should always hold belong in `parameters:`. Both also
live at the same two-level path — `models/<provider>/<slot>.yml` and
`cli_agents/<tool>/<slot>.yml`, beside the `default.yml` that names the provider
or tool in the catalog — so a slot states only what it changes and inherits the
rest, and two slots on one tool can differ.

A definition may also declare `effort_fields:`, describing the settings its
provider accepts (a key, a type, and the permitted values). That declaration is
the only thing the desktop editor reads to build typed controls and to reject an
unknown key at save time, so the editor never learns any provider's vocabulary
and a provider without a declaration simply falls back to editing raw JSON.
Descriptors resolve from the packaged definition when the workspace copy is
silent: they describe the provider, not the scope that happens to hold a file.

`default` and unspecified both mean "do not intervene", but for a *continued*
native session that means "keep the settings this session already has", not
"restore the provider defaults". Session rotation therefore compares a
fingerprint of the settings the adapter will really impose — taken from its own
`applied_settings`, not from the request — so a setting a provider cannot act on
never reads as a change. An empty fingerprint on either side is a request to
keep the session, and only two differing non-empty fingerprints rotate it.
Adapters declare how far a settings change reaches through `settings_scope` —
`turn` (codex re-sends `model` / `effort` on every `turn/start`, so it never
rotates) or `session` (claude, grok).

Effort decisions are recorded through a safe allowlist — requested level,
resolved level, effective model id, the *names* of the applied parameters, and
an unsupported flag — never the effective parameter values, which can sit
alongside API keys and headers. The effective model id and the effort level are
the two deliberate exceptions to "names only": both are values, and both are
written. They surface in trace / diagnostics detail only; the activity history is
about domain outcomes.

What a turn *really* ran on is decided by the layer that holds the provider
knowledge and is carried out on the normal result path: each adapter fills
`AgentTerminalResult.model` / `.effort` with the value its provider reported
(claude's `system/init` model, copilot's confirmed session options, grok's
`initialize` `modelState`) or with the one it imposed itself (codex's validated
`turn/start` settings and the catalog's advertised default model and
`defaultReasoningEffort`, claude's and grok's `--effort` / `--reasoning-effort`
launch flags, `agy`'s command line), and the brain passes
them to `record_span_summary()` and logs one line per span. A continued session
keeps its settings when a turn imposes none, so the conversation record
remembers the last established values (`effective_model` / `effective_effort`,
cleared on rotation) and a claude turn that imposed no effort, or a codex turn
that omitted a setting on a resumed thread, reports what the session still runs
under. A provider that reports nothing and was given nothing leaves both empty
rather than inventing an effective value; the span's model then stays empty
too, while the `agent.slot` / `model.slot` attribute always names the slot so
traces stay searchable whatever model the slot resolved to.

## 5. Command Execution Framework

The generic execution substrate used by workflows and custom commands:

- `drivers/command_runner.py` resolves the target member, builds a `CommandSpec`
  (`commands/models.py`, via `commands/spec_factory.py`), runs child commands
  (`commands:`) first, then the main command.
- Member resolution lives in `runtime/member_context.py`. A run without an
  explicit member falls back to `Team.get_default_person_id()`: the configured
  `default_person_id` (`team/project.yml`), else the first active non-human
  member in person ID order, so only a team without any command-capable member
  raises `PersonSelectionRequiredError`. The default is treated exactly like an
  explicitly named member, so a stale one fails the same way. The App API
  reports the resolved value as `TeamSummary.default_person_id` — the Desktop
  app never picks a fallback member itself, and its default-executor select is
  therefore never empty while the team has a candidate.
- Command types (`commands/registry.py`): `.md` (LLM prompt), `.py`, `.sh`,
  `.yml`/`.yaml` (definition), plus inline commands `print`, `to_html`, `to_pdf`.
- `Context.pipe` carries stdin/stdout-like text between commands;
  `Context.shared_state` carries structured state. Their update order is workflow
  compatibility surface — change with care.
- Config file resolution (`utils/fileio.py`): primary config
  (the selected workspace's `.guildbotics/config`) → package templates.
  Localized files resolve `.<lang>` → `.en` → bare name; person-specific commands
  (`team/members/<person_id>/...`) take precedence over shared ones.
- `commands/discovery.py` is the single definition of resolution precedence
  (extension-outer in registry order, then locale, then location). Every consumer
  — the runtime, the App API `/commands/options` catalog, and the editor's shared
  command list — resolves through it, so they never disagree on the effective
  file. `commands/metadata.py` owns `inputs` / argument / Python-signature /
  placeholder parsing (domain models, not wire models); `commands/validation.py`
  validates source per format. The App API only converts these to wire models.
- Editing shared commands from the Desktop app goes through the Local API, never
  the filesystem plugin. `app_api/command_files.py` owns the editable shared root,
  opaque file IDs (URL-safe relative path; re-validated for root containment /
  extension / symlink on every call), SHA-256 revisions with optimistic
  concurrency, atomic same-directory writes, and a prospective-resolution shadow
  check on create. Persistence accepts syntactically invalid drafts so the editor
  can save work in progress; the execution-readiness guard performs format
  validation and blocks execution with `command_file_invalid_source`. Delete uses
  the same expected revision as update. Errors share one code namespace
  (`command_file_*`).
- Python command metadata lives in a static module-level `COMMAND_METADATA`
  dictionary. Catalog and validation code read it through the AST without
  importing the command, so a Python command, its metadata, its editor revision,
  and its save operation remain one physical file.
- The Desktop command editor's AI assistant separates conversation from mutation.
  Each read-only `/commands/author` request sends the complete current editor buffer,
  latest instruction, permitted operations and effective shared command sources.
  A question returns `action=answer` with no changes and bypasses source validation;
  an explicit modification request returns a reviewed `create`/`update` proposal.
  Existing-file authoring can update only the current command without renaming or
  changing its format, but may create new shared helper/wrapper commands. Desktop
  does not change the editor when either response arrives. Chat retains only a
  short proposal notice; a separate read-only editor workspace displays one
  scrollable tab per current, created, or updated file with its relative path.
  Updated files use a line-numbered inline diff with explicit additions and
  deletions. Only an explicit Apply calls `/commands/author/apply`, and a
  create-only edit proposal leaves the originally selected command open.
  That endpoint preflights every source, target and expected revision before writing
  the change set and rolls back completed writes if persistence fails. Manual
  creation still creates the normal starter source immediately. A stable
  `conversation_id` selects one provider conversation with `resume_policy=auto`,
  while every turn receives a fresh trace ID. The backend retains no chat or pending
  proposal state; the provider store keeps only session metadata needed for
  resumption and traceability.
- Run target guarantee: "save and run" sends the expected file ID + revision.
  Before running, the App API resolves the member once (request person, else the
  team default) and both the guard and the run use it, so the file that is
  checked is the file that runs. It confirms the on-disk revision matches and
  that this member's normal `resolve_named_command()` resolves to that exact file;
  a member / template / higher-priority-extension shadow or a revision mismatch is
  rejected (409 `command_file_shadowed` / `command_file_changed`), never silently
  running a different file. It also validates the selected source before loading
  execution metadata. The execution-status endpoint surfaces the same codes so
  the UI can disable the action ahead of time while leaving Save available.

See `docs/custom_command_guide.en.md` / `.ja.md` for the user-facing guide.

## 6. Member Memory

Members persist knowledge across runs as a document store (mechanism in
`capabilities/member_memory.py`; audit in `capabilities/member_memory_audit.py`).

- **Document**: 1 document = 1 directory (`meta.yml` + `body.md`) under
  `<workspace-data-root>/documents/`. `meta.yml` holds `schema_version`,
  title/summary/keywords, typed `source` entries (ticket/PR/channel/thread URLs),
  creation/update timestamps and member IDs (`created_at`/`created_by`,
  `updated_at`/`updated_by`), `pinned`, and `kind`. Both files stay under the shared
  file size limit, checked together before either is written. A document is text
  only: synchronization carries no binary but member avatars, so anything else put
  beside these two would be held back on every cycle with no way to send it.
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

Path resolution separates machine state from a dedicated GuildBotics workspace
(implementation: `utils/fileio.py`). The workspace root is never inferred from
the process cwd or a member working clone.

| Root               | Location                                                                                         | Holds                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Machine state root | `$HOME/.guildbotics/data` (fixed)                                                                | `active-workspace.json`, `run/service.lock` — state needed _before_ a workspace is chosen                                                                      |
| Workspace root     | `--workspace`, `GUILDBOTICS_WORKSPACE_ROOT`, or the persisted active workspace                   | GuildBotics-only directory. `.guildbotics/config`, `.guildbotics/state`, `.guildbotics/local` live here                                                        |
| Config             | `<workspace>/.guildbotics/config`                                                                | project / member YAML, `secrets.yml` (key names and generations), transcript settings                                                                 |
| Shared state       | `<workspace>/.guildbotics/state`                                                                 | memory documents, chat control state, task-run evidence, activity events                                                                                       |
| Local state        | `<workspace>/.guildbotics/local`                                                                 | diagnostics, transcripts, person leases, chat message cache, member clones, AI CLI sessions, work dirs, hotkeys, `debug.env`                                            |

Invariants:

- Machine state root is always derived from `HOME` and never from the workspace.
- Workspace root is resolved only from `--workspace`, `GUILDBOTICS_WORKSPACE_ROOT`,
  `GUILDBOTICS_CONFIG_DIR` when it is `<ws>/.guildbotics/config`, or
  `active-workspace.json`. Missing workspace is an error for writes.
- `config/` and `state/` are the synchronized tree; `local/` is device-only. Anything
  whose value is decided by the machine rather than the workspace belongs in `local/` —
  absolute paths, but also hotkeys, since which combinations are free depends on the OS,
  the other applications installed, and the keyboard layout.
- `guildbotics workspace migrate --from <checkout> --to <workspace>` copies an old
  source-checkout `.guildbotics/` into a dedicated workspace without changing the
  user's source repository.
- `GUILDBOTICS_WORKSPACE_ROOT` is published at the workspace application boundary.
  Agent execution copies that resolved value into `AgentExecutionContext`.
- There is no `GUILDBOTICS_DATA_DIR` override and no workspace `.env` file.

## 8. Workspace Synchronization

One workspace can be shared by several of the user's machines. `config/` and `state/`
are the shared tree; `local/` never leaves the device it was written on.

**Hub.** A machine the user chooses, holding one bare Git repository per workspace under
`~/.guildbotics/hub` (`guildbotics/hub/host.py`). Every repository is configured
`receive.denyNonFastForwards` and `receive.denyDeletes` on each access: the automatic
reconciliation below rests on the hub refusing to rewind, so a repository restored from a
backup must still refuse a force push. The hub knows nothing about what the records mean,
and stores no workspace paths — only workspace identifiers.

Operations on a hub are performed by the `guildbotics hub` CLI on the hub machine itself,
invoked over SSH by the joining device (`guildbotics/hub/connection.py`). Raw Git commands
are never sent across. Synchronization pins `GIT_SSH_COMMAND` to the OS OpenSSH so that a
host key the user confirmed once is the same one `git fetch` sees.

**Live relay and service owner.** The Hub has two kinds of state outside the Git
repository. `live/<device>/<publisher>.json` is a transient complete snapshot of
one process's current work. The process that runs the work publishes it through
`guildbotics/hub/relay_client.py`; a Desktop backend or `guildbotics start`
process watches the same stream. Heartbeats make delayed and expired states
observable, while expiry deletes the relay file and the viewer drops its cached
snapshot. A newer relay `schema_version` is reported as a client-update error;
it is not silently discarded or partially decoded.

`service-owner.json` is different: it is one persistent owner device per
workspace, managed by `hub owner get/claim/transfer`. Starting a service requires
an owner answer from the Hub and refuses a different owner. The common execution
boundary checks ownership before accepting service work, before the command
starts, and when publishing service-derived live state. A lost Hub connection
does not stop work already in progress, but blocks new service work. An explicit
transfer is allowed only after the user has directly confirmed that the old
service stopped; ownership is never changed by a live timeout. The same boundary
records TaskRun start and terminal state and waits for the corresponding sync
barrier, which prevents a completed input from being accepted again after a
service handoff.

**Workspace Sync Port** (`utils/workspace_sync_port.py`). Storage layers announce a
completed shared write as a `ChangeSet` and never learn that Git is involved. Writes go
through `write_shared_*` / `delete_shared_path`; paths under `local/` are dropped by the
port rather than classified by the caller. `guildbotics/sync` is the only subscriber.

**Git Sync Manager** (`guildbotics/sync/manager.py`). One queue per device runs commit →
fetch → reconcile → push, coalescing bursts. `<workspace>/.guildbotics` is itself an
independent Git repository, and its path is always derived from a verified workspace root
rather than accepted from a caller, so a member working clone cannot be handed to a sync
command. Notifications can be lost without losing changes: a periodic rescan of the working
tree recovers anything the port did not report.

**Concurrent edits.** The hub serializes what is committed. Whichever change lands first is
kept; the later commit is moved to `refs/guildbotics/rejected/<rejection_id>` on the device
that made it, and one provider-neutral activity event records the paths, that device, the
time, and the identifier. This is not an error and does not stop the queue. The set aside
content stays out of every API: recovery is a manual, source-device-only procedure
documented in the README.

**Validation boundary** (`workspace/validation.py`). What crosses the boundary is checked
for three things only: shared root, size limits, decode and syntax; a `schema_version`
newer than this build understands; and the structure of `config/secrets.yml`. These are
the checks nothing else can make — a writer's bug belongs in the writer's tests, and a file
the user hand-edits fails the same way on every device with or without synchronization.
Per-record semantic validators are deliberately absent; adding a shared record needs no
change here.

The generation check only reaches records that declare one, so every record written under
`state/` stamps `SHARED_RECORD_SCHEMA_VERSION` at the point it is written — one constant, in
`utils/workspace_sync_port.py` because the writers span layers that cannot import each other.
A second literal would be a fault rather than a duplication: the boundary runs on the sending
side too, so raising one kind's version alone makes the device that wrote it reject its own
file and stop its own queue. `config/secrets.yml` is the one record that cannot carry a
version, because the index admits nothing but `store_id` and `keys` — the same restriction
that keeps secret values out of the history structurally.

**Joining.** A device with existing content commits it first, then adopts the hub's version
of any file both hold and sends what only it has. It is not an overwrite, and the commit
pushed aside is kept. Joining is previewed; registering is not, because a hub with no copy
of the workspace has nothing to compare against.

**Optimistic locking** (`workspace/config_repository.py`). Config is the shared state a
person edits by hand, so a screen can be composed against content another machine has since
replaced. Reads report each file's Git blob ID; saves state the IDs they expect, and a save
whose expectation no longer holds is refused rather than merged. A screen that reconciles a
whole directory also states the set of paths it read, so a file another device added under
it is a change like any other rather than something the save may prune unseen. This applies
to config alone; for every other shared file the first-committer-wins rule settles it.

**One write API, not a sequence to assemble.** `ConfigRepository.write(apply, expected,
report)` takes the lock, compares, runs the caller's write, and reads the revisions to
answer with — all before releasing. The caller supplies what to write and gets back a
receipt. This is a shape, not a convenience: when the lock, the comparison, the write, and
the observation are separate parts, every writer is a place to order them wrongly or leave
one out, and there are as many such places as there are writers. Each half of it earns its
place. Writing inside the comparison's lock stops a refusal that has already applied half
of itself. Reading the revisions there too stops an answer that pairs content the caller
wrote with a revision the hub's arrival produced — the screen would save against it and
overwrite content it never saw. Every route that changes config goes through
`app_api/config_revisions.py`, including those with nothing to compare: comparing is
optional, being the only writer while writing is not. Which routes those are is not
decided by the URL — commands (`config/commands`) and transcript settings
(`config/transcripts.yml`) are config reached from elsewhere, and the `guildbotics
secrets` CLI writes `config/secrets.yml` from another process entirely.

**The shared-write lock** (`utils/shared_write_lock.py`). One lock per workspace, taken
by every writer of its shared files and by the synchronization queue across checking the
hub's content out and committing what survives. Without it the comparison decides nothing —
a save can land on top of content adopted while it was running, and since that overwrite is
an ordinary local write, the next cycle commits and pushes it with nothing recording the
other device's change as lost. Nothing holds the lock across the network, so a save never
waits on a hub. A wait that runs out raises `SharedWriteBusyError`, deliberately outside the
`OSError` family: synchronization catches that family to mean the environment failed and
would otherwise report the hub unreachable over a local save.

Which writers must take it is not decided writer by writer. Every write to a shared path
goes through the sync port, and the port takes the lock for it, so a writer that composes
what it writes without reading anything has nothing to declare and no way to forget.
Device-local paths and an unselected workspace are dropped by the same judgement that
decides whether to announce a change, so neither waits on anything.

What the port cannot infer is how far back a span reaches. Config is not the only side that
loses this way: conversation control state (`state/chat_state`) and member memory
(`state/documents`) read a shared file and write it back without knowing Git exists, so the
same interleaving reinstates what the queue just adopted — costing a Slack answer, or a
whole memory document. `update_shared_text(path, apply)` and its JSON form take the lock,
read, hand the content to `apply`, and write what comes back, so the span starts at the read
by construction rather than by each writer remembering to say so. It is the same shape as
`ConfigRepository.write` for the same reason: passing the transformation in leaves no
sequence for a caller to assemble wrongly.

A few operations are wider than any single write — a journal that may be replaced rather
than appended to, a document that is two files, a policy that is at most one per directory —
and those take the lock themselves. Within one thread it re-enters, so such a writer declares
its span without knowing whether a caller declared a wider one; that question is what was
being answered per writer, and wrongly. Optimistic locking stays with config alone: the lock
closes the silent loss, while a conflict the queue records as first-committer-wins is the
design working.

**What the boundary commits is what it validated.** Content is staged first and checked
from the index. Reading a file to validate it and letting `git add` read it again are two
reads of something a writer can change in between, and the second is what becomes history —
one such file stops every receiving device's queue, while the sender stays green because its
working tree matches the commit it made. Staging first also settles a deletion recreated
before the commit: the recreated file is staged as content, so it is checked as content. A
file that fails is unstaged and held back, left on disk for the user to fix.

**The interval no writer can protect.** Fetching waits on the hub without the lock, and a
save made in that interval holds the lock correctly and is still only in the working tree, so
the checkout would take it with nothing recording the loss. The queue's convergence therefore
holds the lock from end to end — nothing in it waits on the hub — and re-runs the commit
boundary as its first act: the save becomes a change with a name, which either survives the
adoption or is rejected on the record. A commit there means the convergence was computed from
a head that no longer exists, so the cycle is redone. Joining a hub has the same interval,
for the same reason, and closes it the same way — the join commits again inside its own lock
and compares against the head that produced.

**Queue ownership.** The Desktop backend and `guildbotics start` may activate a queue;
member CLI writes use a short-lived queue boundary after the capability completes. If
another process holds `local/run/sync.lock`, the member command reports `sync: pending`
and exits successfully; its next invocation or the queue will recover the working-tree
commit. `sync/activation.py` keeps one queue per process, while
`local/run/sync.lock` serializes each cycle, one-shot commit/push, and workspace pause
across processes. The machine-wide `run/service.lock` is the background-service exclusion,
not the repository lock.

## 9. Secret Storage (SecretStore)

Secrets = LLM provider API keys (`models/<provider>/default.yml` `api_key_env`) and
person secrets (`GITHUB_ACCESS_TOKEN` / `GITHUB_PRIVATE_KEY` / `SLACK_BOT_TOKEN` /
`SLACK_APP_TOKEN`). Non-secret GitHub App IDs live in `person.yml` `account_info`.

- **Backend** (`utils/secret_store.py`): OS keychain only. The workspace keeps a
  non-secret index in `.guildbotics/config/secrets.yml` (`store_id`, logical key
  names, generations) and device generations in `.guildbotics/local/secrets.json`.
  There is no env-file backend and no `GUILDBOTICS_SECRETS_BACKEND` switch. A
  missing or locked keychain is an error. Machine moves use `guildbotics secrets
  export` / `import` (dotenv is an exchange format only). `secrets set --from-file`
  absorbs a PEM or other file into the keychain.
- **Keychain adapters** (`utils/keychain.py`): `KeyringSecretStore` owns the
  workspace index and delegates value I/O to a platform adapter. macOS and Linux
  retain the Python keyring backend. Windows uses `utils/windows_credentials.py`
  and writes UTF-8 opaque blobs to a GuildBotics-specific Credential Manager
  namespace, preserving the 2,560-byte capacity for ASCII-heavy PEM keys. Batch
  imports validate every value before the first write.
- **Resolution priority** at process start: real environment variables > OS keychain.
  Allowlisted debug settings (`LOG_LEVEL`, `AGNO_DEBUG`) come from
  `local/debug.env`. GuildBotics never reads a workspace `.env`.
- **Exception**: `*_GITHUB_PRIVATE_KEY` (GitHub App PEM content) is _never_ injected
  into the environment. Consumers read it on demand through the secret store.
  Generated registration PEMs are written under the OS temporary directory and
  deleted after the keychain absorbs them.
- **Tests**: an autouse `fake_keyring` fixture installs an in-memory keychain so
  tests never touch the developer's real OS store.

## 10. Observability and Diagnostics

`guildbotics/observability/` is the recording substrate (depends only on `utils`):

- **Correlation**: `trace_scope` / `span_scope` / `correlation_fields` /
  `set_attributes` correlate everything that happens in one execution — a manual
  command, one scheduler cycle, one incoming chat event — under a single `trace_id`,
  with spans for LLM / AI CLI tool calls and `call_id` correlation.
  Trace attributes carry structured search keys (e.g. `github.url`, `github.number`).
  Idle ticket patrols (a routine cycle whose selector finds no actionable ticket) open
  no trace and leave no records; liveness is exposed instead as a per-member patrol
  heartbeat (`member_routines`) in the scheduler's runtime status.
- **Persistence**: `run/diagnostics.jsonl` is a small searchable index containing session
  pointers, execution boundaries, span summaries, and domain events. Complete events, logs,
  spans, and request/response I/O are stored as one JSONL transcript per execution or system
  session under `run/sessions/` (`session_transcripts.py`). `standard` detail omits high-volume
  thinking/delta events and bounds AI CLI tool stderr to its final 8 KiB; `full` keeps them.
  Session transcripts default to 30-day retention. Memory audit remains a separate bounded
  store under `documents/memory_events.jsonl`.
- **Consumption**: `app_api` reads the index, selected execution transcript, and memory
  audit, and converts provider payloads into provider-neutral activity
  events/links/titles for the desktop Activity History (`activity_events.py`,
  `activity_links.py`). Display normalization lives _only_ there — not in
  observability, not in the frontend. Manual desktop command runs (`source: manual`)
  are excluded from the session timeline because they fire constantly; anything they
  changed still appears as an activity event. Desktop AI assistant turns record under
  that same source for the same reason — the agent work kind scopes the provider
  conversation, not the trace — so they are filterable in diagnostics and absent from
  the timeline. Diagnostics keeps every source.
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

- **Read-only diagnostics queries**: `guildbotics diagnostics traces | trace <id> | system`
  (`guildbotics/cli/diagnostics.py`) reads the index and transcripts through
  `DiagnosticsStore` and emits raw records as JSON. It is a top-level group rather than
  a `member` subgroup because a diagnostics query is workspace-scoped, not member-scoped:
  it needs no acting member, takes no execution lease, and `--person` is a filter over the
  records' `person_id`, not an identity. It never calls `start_maintenance()`, so reading
  cannot rotate or prune what it reads, and it carries no display normalization — record
  meaning is taught to its consumers instead. `--query` searches each candidate's full
  transcript, not just the index, because the text a failure describes itself with lives
  in log and I/O records that the index never stores. Desktop assistant traces are excluded by
  default so the troubleshooting agent does not investigate its own conversations.

## 11. Desktop App

`desktop/` is a Tauri v2 + React (Mantine, TanStack Query) frontend; the repository is
a monorepo on purpose.

- **Local API boundary**: the GUI never reimplements the Python engine. It launches the
  bundled backend (`python -m guildbotics.app_api`, FastAPI) and talks to it on
  `127.0.0.1` via REST + WebSocket. Every request requires the per-process
  `X-GuildBotics-Session-Token`; `/health` is the liveness probe. The launcher mints
  that token and hands it over through `GUILDBOTICS_APP_API_TOKEN` — never argv, which
  `ps` exposes to every other user on the host — and the server refuses to start
  without it, consuming the variable on startup so the AI CLI agents it spawns from a
  copy of its environment never inherit the token. Browser origins are equally
  launcher-supplied: CORS allows the Tauri origin plus whatever
  `GUILDBOTICS_APP_API_ALLOWED_ORIGINS` lists, so a page on an arbitrary localhost
  port cannot reach the API.
- **Runtime lifecycle**: the sidecar manages member workers / event listeners inside
  its own process and reports states to the UI. Desktop and CLI `guildbotics start`
  use the same machine-wide `service.lock`, so only one background service can own
  scheduler workers / event listeners at a time. CLI `start` remains the headless
  equivalent.
- **Packaging**: `scripts/desktop-build-backend.sh` builds two PyInstaller sidecars
  (`guildbotics-app-api`, `guildbotics-cli`) into `desktop/src-tauri/binaries/`. The
  app also installs a managed `guildbotics` CLI shim and the GuildBotics skill for
  interactive agents. External AI CLI tools are _not_ bundled — the GUI detects,
  verifies, and configures them only.
- **Sync / Device and hub**: two settings sections (`desktop/src/sync/`). "Sync" covers
  connecting, reconnecting to a rebuilt hub, the changes that cannot be sent, and the
  devices that joined; "Device and hub" holds what belongs to the machine rather than to
  a workspace — hub hosting and this device's SSH key — and is listed in the settings
  nav under its own "This machine" group. A sidebar indicator derives one of seven states
  from `GET /workspace/sync`, ordering them so that what waiting will not fix comes first;
  only those states also reach the app-wide warning band. Connecting is a sequence rather
  than one button because each step means something: the hub must be reachable before its
  workspaces can be listed, the host key is confirmed by a person (synchronization runs SSH
  non-interactively, so this is the only opportunity), and joining an existing workspace is
  previewed while registering a new one is not. Hosting the hub and this device's key belong
  to the machine rather than to a workspace, so the section is reachable — and those two
  cards are shown — before any workspace is selected; only the sharing itself waits for one.
  The machine that hosts the hub is usually the one that never gets a workspace of its own.
- **Config saves are explicit and revision-checked**: every settings screen saves on a
  button, and sends back the revisions its read reported. A refused save reloads the screen
  instead of resending, since resending is exactly the overwrite that was refused.
- **Global hotkeys and the quick-run window**: assignments are device-local
  (`.guildbotics/local/hotkeys.yml`, served by `GET`/`PUT /hotkeys`) because which
  combinations are free is a property of the machine, not of the workspace.
  `guildbotics/app_api/hotkeys.py`
  owns the accelerator grammar and the conflict rules; the Tauri host only registers
  what the frontend hands it. Pressing a combination reads clipboard text or an image
  and opens a resizable frameless window (`quick.html`); clipboard images are persisted
  through the same session-scoped input-file store used by paste. The generic hotkey
  offers a command picker, while a per-command hotkey runs straight away unless a
  required input is missing, in which case the window waits. Because hotkeys only fire
  while the process lives, closing a window hides it and the app stays resident in the
  menu bar; quitting goes through the tray, which is where the "work still running"
  guard now lives.
- **AI assistants**: the command editor and the diagnostics screen each host a
  conversational assistant. Both share one substrate:
  `guildbotics/intelligences/assistants.py` opens a resumable, structured turn (one JSON
  payload in, one typed model out, keyed by a stable `work_identity` so the provider
  resumes its own session), and `AppRuntime._assistant_turn` resolves the acting member,
  tracks the turn as cancellable manual work and correlates it under a fresh trace.
  A turn that is declared read-only is confined by the provider, not by its prompt: the
  Claude adapter drops `bypassPermissions` for an explicit tool allowlist, the Codex
  adapter forces a `read-only` sandbox with no network, and `cli_agent` takes no member
  execution lease. Only such a turn is tracked non-exclusively, so it stays usable while
  that member runs scheduled work — which is exactly when its logs are worth asking
  about. Command authoring is also read-only: it can inspect the effective shared
  command sources and return structured proposals, but only the separate explicit
  apply endpoint writes them. On the frontend,
  `desktop/src/assistant/` owns the shared chat panel and conversation state; each screen
  keeps its own mutation. Conversations are never persisted on this side.
- **Troubleshooting assistant**: opened from the diagnostics screen, it answers questions
  about recorded executions. It is given only the question and the focused view; it
  gathers its own evidence by running the read-only `guildbotics diagnostics` commands,
  which keeps prompts small and lets it follow leads into other executions. What it reads
  is untrusted — logs carry GitHub issue bodies, chat messages and external tool output —
  which is why the read-only confinement above is enforced rather than requested. It returns
  the answer plus the trace ids it read, and the backend drops any id that was never
  recorded before the frontend turns it into a link. The record detail and the assistant
  share one mutually exclusive right-edge panel, so two drawers never fight over the same
  strip; the assistant is non-modal, so the executions list behind it stays clickable.
- **Support targets**: macOS Apple Silicon (DMG) and Linux x86_64 (`.deb` / AppImage).
  The CLI remains the fallback everywhere else.

See `desktop/README.md` for build, development, and test instructions.

## 12. Domain Model Notes

Core entities live in `guildbotics/entities/` (`Team`, `Person`, `Task`, `Message`).
Two Person distinctions matter architecturally:

- **AI members** are execution subjects: active members get a scheduler worker, Slack
  subscriptions, credentials, and an intelligence (brain) configuration.
- **Human members** are _references to real people_ (roles, Slack user id, GitHub
  username, avatar) used for handoff candidates and assignee mapping. They are saved
  with `is_active: false`, are never scheduled, hold no bot/app credentials, and the
  desktop settings UI intentionally hides all agent-execution fields for them. Treat
  any path that would execute a human member as a boundary bug, not a feature. The
  rule is enforced in one place, `ensure_execution_subject()` in
  `guildbotics/runtime/member_context.py`, applied by command execution
  (`guildbotics/drivers/command_runner.py`) and by member capability resolution
  (`resolve_member_context()`). Every `guildbotics member ...` command that acts as
  a member resolves it that way, so the rejection happens before any capability
  runs; the commands that do not act as a member (`member help`, and
  `member task status`, which ignores `--person`) never resolve one.

## 13. Extension Points

- **New brain**: implement `Brain`, register via the intelligence mappings
  (`guildbotics/templates/intelligences/*.yml`).
- **New AI CLI tool**: implement a native adapter under
  `intelligences/agent_runtime/`, register it in `agent_runtime/factory.py`, and add
  its catalog entry to `CLI_AGENTS` in `intelligences/cli_agents.py` with a matching
  `templates/intelligences/cli_agents/<tool>/default.yml`. There is no YAML-only path:
  a tool without an adapter cannot run. The user-configurable filesystem boundary, for
  the adapters that can enforce one, is declared in
  `templates/intelligences/native_agent_policy.yml`.
- **New command type**: subclass `CommandBase` with `extensions` / `inline_key`; the
  registry picks it up (`commands/registry.py`).
- **New integration**: implement `TicketManager` / `ChatService` and wire it in the
  edition's `SimpleIntegrationFactory`.
- **New edition**: implement `Edition` (`get_context()` / `get_default_routines()`)
  and select it via `GUILDBOTICS_EDITION`.
- **New activity event/link kind**: add the recording payload (capability →
  observability), the normalizer/API model (app_api), and the frontend rendering
  (desktop) as three separate responsibilities.

## 14. Related Documents

- `AGENTS.md` — working rules for this repository: responsibility boundaries, CI
  commands, testing strategy, prompt layer model.
- `README.md` / `README.ja.md` — user-facing setup and usage.
- `docs/custom_command_guide.en.md` / `.ja.md` — custom command development guide.
- `desktop/README.md` — desktop build/dev/test guide.
