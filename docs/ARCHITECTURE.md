# GuildBotics Architecture

**Audience**: Developers and AI agents working on this repository.
**Scope**: System-wide concepts and boundaries. Source code is authoritative; when this
document and the implementation disagree, fix the document (see `AGENTS.md`).

---

## 1. System Overview

GuildBotics runs AI agents as _team members_ that collaborate through Branvil and Slack.
A scheduler starts one worker per active member; workers pick up work from Branvil
(tickets) or from incoming Slack events, then delegate the actual
investigation/editing/judgment to an external AI CLI tool (Claude Code, Codex CLI,
Antigravity CLI, ...). External side effects that the AI agent performs _as the member_
— git pushes, Branvil comments and PRs, Slack posts, memory writes — go through a single
boundary: the `guildbotics member ...` CLI (member capability). Workflows additionally
perform a narrow set of orchestration/status writes of their own (see §3).

Users interact with the system through:

- **Screens** (`desktop/`, React, served by the server to a browser): setup, service
  runtime control, activity history, and diagnostics over a Local API.
- **CLI** (`guildbotics ...`): headless start/run/stop, workspace selection, secrets
  migration, and the member capability entry point.
- **Interactive skill** (`skills/guildbotics/SKILL.md`): lets a user-driven coding agent
  work as a configured member in the user's current repository.

## 2. Package Map and Dependency Rules

```text
guildbotics server start                 Rust host, one process on the Hub
  ├─ GuildBotics screens and /-/guildbotics/api        gb-server, loopback port 7878 by default
  ├─ Branvil screens, Git and HTTP API   branvil, loopback port 7878 by default
  └─ one microVM per command            gb-environment
       └─ guildbotics.guest.entry       Python commands, subcommands and AI CLI turns
```

`gb-config` resolves workspace settings, team and member data, command names and
declarations, and brain slots. `gb-server` uses those results to start a command
and write its records through `gb-observability`. Member operations live in
`gb-member` and use the `gb-ports` contracts. `gb-integrations` selects the
provider, including the embedded Branvil API. Branvil has no dependency on a
GuildBotics crate. `gb-setup` validates setup changes, `gb-workspace` owns
workspace selection and identity, and `gb-base` owns the shared file and path
operations used across crates.

The host starts `/opt/guildbotics/venv/bin/python -m guildbotics.guest.entry` with
the resolved command and a read-only file of command facts. The guest uses the
resolved command table when a subcommand names another command. Python owns
template rendering, command sequencing, and provider CLI protocol adapters.
The wire models in `guildbotics/intelligences/agent_runtime/wire.py` generate the
[schema](../wire/README.md) read by `gb-wire`.

Python dependency contracts in `pyproject.toml` keep shared packages independent
of the guest entry and host-only libraries. `tests/guildbotics/test_package_map.py`
also loads the brains and bundled commands that are imported by name. See
[AGENTS.md](../AGENTS.md#実装と責務境界) for ownership and verification rules.

## 3. Member Capability Delegation Model

The central design of GuildBotics is a three-party delegation chain, identical for the
ticket workflow, the chat workflow, and interactive skill sessions:

```
workflow (orchestration)
  -> AI CLI agent (judgment)
     -> trusted member transport
        -> guildbotics member ... CLI (external side-effect boundary)
```

- **Workflow** (`guildbotics/templates/commands/workflows/*`): builds the prompt
  payload for the selected work item and asks for one CLI agent turn with the member
  workspace as cwd. `CommandRunner` drives the turn (`commands/agent_turn.py`) when
  the execution context names `max_completion_attempts`: it retries the turn with a
  continuation prompt until the member records its completion and returns the
  response. It runs inside the command's isolated environment and reaches the run
  record only through the command's window to the host (`ClientRunLedger` in the
  environment, answered by the run-ledger handler in `gb-server/src/host_window.rs`, which locates
  the record from the host's own workspace). The workflow never reads or writes the
  run record itself, and it never produces work products
  (code changes, PRs, replies, review comments) and makes no writes of its own: the
  host selection around the turn — the ticket selector (`gb-server/src/ticket_selector/`)
  and chat selection (`gb-server/src/chat_dispatch.rs`) — moves the board lane and
  posts the rate-limit / failure status on the ticket or in the Slack thread.
- **CLI agent**: reads the ticket/thread, investigates, edits, and decides. It never
  receives Branvil or Slack tokens and never calls provider APIs directly; every external
  write goes through `guildbotics member ...`.
- **Trusted member transport**: interactive skill sessions invoke the member CLI
  directly. Every native provider session instead receives one authenticated MCP tool,
  served on the host and reached from the isolated environment, that can invoke only
  the fixed member CLI for the active person, workspace, and turn. The broker runs each
  command inside the host's GuildBotics process that runs the command (the server,
  or a local `guildbotics run`), on a worker
  thread with its own working directory, streams, and invocation, instead of starting
  a CLI process per call. The broker, not the provider process, holds the turn's
  execution lease and a separately rotated per-turn grant; a command that outlasts
  the broker's timeout is reported and left to finish. Relative paths in a command
  resolve against the agent's isolated working directory.
- **Member capability** (`gb-member` and `gb-server/src/member/`):
  the boundary for member actions decided by the AI agent — the only path through
  which the agent's provider operations run and the only layer that resolves the
  member's credentials for them. It records run evidence (`gb-observability/src/runs.rs`) for each
  side effect.
- **Repository inspections** (`repository/issue_inspect`, `repository/pr_inspect`,
  `repository/pr_checks`): bundled Python commands combine the pages returned by
  the common code-hosting service. The existing member broker carries each read;
  the host selects the matching provider operation, validates continuations,
  and records observed work targets. The code host's `readiness()` (judged by
  `gb-integrations/src/readiness.rs` from what the provider reads) is the one readiness, shared by the
  inspection, push, and completion paths. An agent already
  inside a command calls `/opt/guildbotics/venv/bin/python -m guildbotics.guest.entry <name> <args>`
  to run a child in that environment under the main command's access contract.

Trust rules that follow from this shape:

- The workflow does not trust the agent's natural-language stdout as the result. Only
  the completion record (`member task complete` / `member chat complete`) and the run
  evidence store are authoritative.
- A reviewer finishes with `done` after submitting a verdict, including request
  changes. An author uses `asking` only when a reply is needed to continue.
  For `asking` or `blocked`, `--waiting-for` records the next member's roster
  person id. Validation checks the targets of the run's recorded comments, not
  its entry point: a ticket run can ask a reviewer on its PR. An agent must be
  reachable by the patrol on at least one such target; a human member is
  accepted without a patrol or credentials. Only waits entirely for active
  agents suppress the owner notification. When no recipient is named, the CLI
  explicitly reports that the owner will be notified.
- Ticket completion with `status=done` resolves the pull requests the run wrote to (run
  evidence), the ticket PR, and the code host's PRs that refer to the ticket
  (`ticket_pull_requests`), each only when the member authored it (the patrol resumes a
  member only for its own PRs), then revalidates each open PR against the code host's current state;
  closed and merged PRs are not readiness targets, while their CI rollup, checks, and failed
  logs remain available for inspection. Completion is rejected when observed CI fails or
  remains pending, the host requires an up-to-date head and the head is behind its base,
  or the head/base changes during the check. `behind_by` and `out_of_date` report the
  measured lag even when it does not block completion; `completion_blockers` names what
  must be resolved. Check `details` preserve the host's explanation, including merge
  conflicts without a failed job log. `rollup=no_checks` stays non-blocking when the
  current base also has no checks, while checks on the base make an empty head a
  registration-pending blocker.
  Interactive push/publish results expose compact readiness and blockers for matching open
  PRs; readiness lookup failure, including an HTTP transport failure, does not turn a
  successful push into a failed command.

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
(「メンバー・provider・実行」).

### External service ports

A member works with external services through four ports, chosen per kind by
`project.services` (`gb-integrations/src/factory.rs`). A port is the contract of operations
and data a caller understands without knowing the service; a provider maps it onto a
service's API.

| port | responsibility | called by |
| --- | --- | --- |
| `CodeHostingService` | repositories (git remote, default branch, push credential and scope, commit URL), pull requests (reads and writes, conversation comments, reviews, inline comments, replies, reactions), CI (readiness, failed logs, artifacts), repository information, the pull request patrol, the pull requests that refer to a ticket | `member pr / repo-info / git`, task completion, the patrol |
| `TicketService` | tickets (reads and writes, a created one put in the board's new lane, comments, reactions, the files their bodies and comments refer to) and the board (candidates, re-reading, lanes, closed work) | `member ticket`, the ticket selector, the activity import |
| `ChatService` | the chat: identity, history, posts and replies, reactions | `member chat`, the chat workflow |
| `EventListener` | receiving the chat, one connection at a time | the chat receiving runner |

```mermaid
flowchart LR
    CodeCLI["member pr / repo-info / git<br/>task completion"] --> Code["CodeHostingService"]
    TicketCLI["member ticket"] --> Tickets["TicketService"]
    Patrol["ticket and PR patrol"] --> Code
    Patrol --> Tickets
    ChatCLI["member chat"] --> Chat["ChatService"]
    Receive["chat receiving"] --> Listener["EventListener"]
    Receive --> Chat

    Code --> Branvil["branvil"]
    Tickets --> Branvil
    Chat --> Slack["slack"]
    Listener --> Slack
    Code --> Local["local"]
    Tickets --> Local
    Chat --> Local
    Listener --> Local
```

Shared contracts live in `gb-ports`:

- **People.** Ticket assignees, PR authors and requested reviewers, and comment,
  review and thread authors use `ActorName`: a service account name, not a
  GuildBotics person id. Each provider's descriptor exposes a credential-free
  `member_identity(person)` with its public names (including API aliases) and
  mention spelling. The patrol and author classification use that same identity.
  `member context` publishes the team roster so agents can resolve these names
  and address a teammate without guessing an id from an account name. The local
  reference provider deliberately gives members names different from their ids.
- **References.** Tickets and pull requests are named by their URLs; a read is
  `read(resource, subject)`, the subject being the item's URL or, for repository
  information, the repository's path, which the provider checks and spells (Branvil
  `workspace/repo`). Comments, reviews, threads, CI jobs, artifacts, and attachments are opaque string
  ids that mean something only together with their item's URL. A work target is
  `{kind: ticket | pull_request, url, title, label}`, `label` being the short reference
  the service shows it by (Branvil `#12`); shared code never builds one from a number. A
  ticket has a `key` (its branch is `ticket/<key>`), a `tracker` (where it is kept), the
  `repository` it belongs to, if any, and its `lane` on the board.
- **Failures.** `ServiceError` (a message fit for the user, never what the service
  answered or a credential), `ServiceNotFound` (not there, or not this service's), and
  `ServiceUnsupported` (the service cannot do it at all). Optional features — reactions
  by meaning, `request_changes`, a ticket created off the board, artifacts, a ticket's
  attachments, failed logs,
  `dependency_alerts`, the pull requests of another service's ticket, a link that closes
  a ticket on a merge — raise `ServiceUnsupported` when called; an empty result means
  read and found nothing.
- **Reactions** are four meanings (`ack`, `agree`, `celebrate`, `support`) on every
  port; a provider never substitutes another reaction for one it lacks.
- **Reviews** take a verdict (`approve`, `request_changes`, `comment`) and, like inline
  comments, the head the member read: on a moved head nothing is written.
- **A workflow status** (`WorkflowStatus`) is what a run that did not finish leaves where
  its work came from; each provider carries it in its own form (a hidden comment marker,
  message metadata) and gives it back on reading. It also marks the notice a member posts
  in its explicitly selected chat channels (`chat.notify_work_stops`) when its ticket or PR work stops to wait for a person, which the
  ticket selector posts (`gb-server/src/work_stops.rs`), so no member answers it.
- **Write scope.** Each service's configuration names a scope (a workspace in Branvil); a
  write goes only to a repository or tracker directly in it (`check_path`), a push only
  to the code host's.
- **Selection rules are GuildBotics' own.** The shared patrol code decides which pull
  request asks something of a member (`gb-integrations/src/patrol.rs`), whether a
  head may be reported complete (`gb-integrations/src/readiness.rs`), and whether an assigned
  ticket is a member's work now (`gb-integrations/src/selection.rs`) by applying these
  shared rules to snapshots read by the providers. The services keep their own data, and
  the reference provider `local` goes through the same rules.

`branvil` (`gb-integrations/src/factory/branvil/`) is a code host and tickets of a
[Branvil](branvil_integration.en.md) service that every machine reaches at the same
base URL. Its API answers raw data (comments with an opaque `meta` carrying a workflow
status, reviews, threads, checks); the provider applies the rules above to it.

## 4. Workflow Invocation

`gb-server/src/dispatcher.rs` opens the execution boundary for each work item
with its member, command, source (routine, scheduled, event queue or manual),
trigger, run ID and work identity. Selection happens before dispatch.

- **Ticket workflow** (`workflows/ticket_driven_workflow`, the default routine,
  `TICKET_WORKFLOW_COMMAND`): a routine that _polls_ the configured board (Branvil) and the code host's pull requests. The board is treated as a loose
  queue/trigger — GuildBotics does not reimplement fine-grained code-host/git
  operations around it, and there is no webhook receiver (local-first design). The
  candidates are the pull requests that ask something of the member
  (`CodeHostingService.pull_request_candidates()`), then the board's tickets
  (`TicketService.candidates()`), and each is re-read from the port it came
  from before it is dispatched; a failed or rate-limited run's status comment goes to
  that port too.
  Unanswered mentions on open tickets outside the member's assignment are also
  candidates, even outside the ready/working lanes. Providers resolve mentions;
  `ticket_selection.mention_pending` considers a member's subsequent comment an
  answer. These runs use `Work("ticket_mention", ticket_url)`: a separate conversation
  that posts a comment and completes, without preparing a repository, publishing,
  creating a PR, or moving the ticket. A response affecting the assignee's work
  mentions that assignee. An external comment addressed only to other members
  is excluded only from the assigned work's decision history. The task keeps the full
  conversation; initial work, unanswered summons, held status and linked-PR completion
  still use their own evidence. Comments without mentions keep the ordinary behavior.
  Branvil returns resolved mentions with the body or comment and supports a
  workspace query for tickets that mention the member. The local provider
  scans its files. Assignment and unanswered mentions are rechecked before
  dispatch.

  Every route that runs it — the patrol, a scheduled command, a manual run
  (`guildbotics run`, the screens) — selects the ticket in `gb-server/src/ticket_selector/`
  and runs the workflow through `TicketSelector.run`, which owns what surrounds the
  AI CLI turn: moving the ticket to the working lane, the run id and completion
  budget (`GUILDBOTICS_TICKET_MAX_ATTEMPTS`), the trace attributes that name the
  ticket, and the status comment of a failed or rate-limited run. The patrol selects
  outside any trace and dispatches each candidate in turn; a scheduled or manual run,
  or a routine that names the workflow with arguments, takes the ticket the patrol
  would take next. The workflow only runs the turn. No run waits for CI in its turn,
  whatever its work (a ticket, a chat thread, a command): when pending checks are all
  that blocks its own PRs, the member ends the run with `member checks await`, which
  leaves an `awaiting_checks` status on each such PR naming the run and its work
  (`Work`'s kind and identity, from the run's grant), and records the run's completion.
  The patrol starts a `pull_request_checks` run once the checks settle or the head
  moves; while the wait lasts, any task the PR asks for (feedback that arrives
  meanwhile too) runs in the work the status names (`Task.resumes`), so the run
  continues that work's conversation and reports where the work came from, with the
  awaiting run's summary as far as this device has it. `TicketSelector.run` ends the
  wait with a `checks_settled` status unless the resumed run awaits the checks again.
  Behavior that belongs to a run answering a chat event keys on that event, not on
  the work's kind, since a resumed run in a chat thread's work answers none.
- **Manual runs**: a command a person starts runs inside one boundary, whether the
  screens run it  or a local `guildbotics run` does
  (`crates/guildbotics/src/main.rs`): a `manual` trace, an execution record
  (the task-run record's start and finish, and the member's lease unless the command
  declares itself read-only), and the command's `command.started` /
  `command.finished`. The run is its trace (`run_id == trace_id`).
- **Chat workflow** (`workflows/chat_conversation_workflow`): the events the project's
  chat (`project.services.chat_service`) delivers to its listeners and backfill are
  persisted as pending events by `gb-server/src/chat_receiver.rs`, then drained per
  member by `gb-server/src/chat_dispatch.rs`. The runner holds one listener per
  connection the provider names (`listener_key`: Slack's per app-level token), and
  places messages in time by their `occurred_at`; a message's `position` (its UTC
  time at fixed width, then its id) orders a channel's messages as text, so pending
  queues, wake cursors, and the AI CLI session's context cursor compare positions. Like the ticket
  patrol, the dispatcher selects before it starts a workflow: selection
  (`gb-server/src/chat_dispatch.rs`) reads the thread, drops events that are not work
  for the member, judges the rest, and settles reaction-only and no-op outcomes itself.
  The workflow only runs the AI CLI turn for events that need a response; selection
  owns the conversation ledger around it (read marks, the thread cache, thread state,
  the run record, handoffs, failure notices).

Invariants:

- Scheduler-managed work is serial per person: one task per active member
  (`gb-server/src/native_scheduler.rs`) runs scheduled, routine, and queued-event work one at
  a time. The member lease serializes non-read-only runs that use a member's
  clone across manual commands and separate GuildBotics processes with one OS advisory
  lease per person. The execution boundary in `gb-server/src/execution.rs` takes the lease;
  every host run goes through it (the scheduler's work, and a manual command whether
  the screens or a local `guildbotics run` starts it). Interactive `member` commands
  take no execution lease: shared state and git operations have their own locks,
  and interactive sessions cannot use the member's clone. Nested `member` writes
  require the exact delegated lease/run identity; environment-variable presence alone
  never authorizes them.
  Read-only `member` commands declare that capability in the member command catalog;
  undeclared commands fail closed as write-capable instead of being classified from
  command-name strings.
- `guildbotics server start` runs both services in one foreground process
  when Branvil has been initialized. The host holds
  `<machine-state-root>/run/app-api.lock` until the process exits. A second
  server using that root refuses to start. `service.lock` covers scheduler
  workers and listeners only while they run. A lock file's presence alone
  does not establish liveness. For shutdown order and signals, see
  [Screens and Server](#11-screens-and-server).

- A member that finishes work while messages 3 and 5 arrive in one Slack thread
  reads the unread conversation through message 5 in its next run. The oldest
  queued event starts that run; selection combines queued messages with a fresh
  provider snapshot, including intervening conversation and corrections. Historical
  context is bounded to 100 messages; unread input is kept separately so that the
  context bound cannot discard an earlier request. Both fresh and resumed AI CLI
  sessions receive the whole unread batch.
- Before judging the batch, selection records its event IDs and last
  message timestamp as `chat_batch` evidence on the task run. Completion acknowledges
  and removes those IDs. Additional `chat_updates` input is acknowledged only on
  done/asking after a subsequent chat reply, post, reaction, Branvil write, or Git publication. Blocked runs and updates
  with no later external action remain queued for another run.
  Messages not delivered to the agent also remain queued. The dispatcher reloads the thread on its next pass. Recovery
  after a recorded completion uses that saved membership, not a newly fetched
  thread, so it cannot acknowledge unread messages or repeat completed actions.
- Before replying, reacting, pushing Git commits, or writing to Branvil, a chat agent
  calls `guildbotics member chat updates --person <person>`.
  The command returns `new_messages`, `up_to_date`, `catching_up`, or `unavailable` from the durable
  queue for the run's original thread, which is the run's work as the host settled it,
  not anything its record says, without calling Slack APIs. The agent reads
  new messages and reconsiders the work. Capability write boundaries independently
  reject missing checks, unseen input, and unavailable reception. Interactive and
  ticket runs have no chat precondition.
- Socket activity wakes a separate receive task, so persistence does not wait for
  the five-second backfill scheduling cycle or an in-flight backfill request. Failed
  persistence retains the drained events for retry. Socket and backfill writes
  acquire the shared lock without blocking the event loop; contention yields and
  retries. Until received messages are saved, `catching_up` prevents publication
  and tells the agent to wait and recheck, without completing as blocked. A
  device-local heartbeat updates on state changes or every five seconds while
  unchanged; a disconnected receiver, absent record, or heartbeat older than
  15 seconds makes checking unavailable. Checking and external writes
  are separate operations: messages can still arrive after the final check.
- A failed chat run retains its queue head and retry budget. Its next attempt
  refreshes unread input, allowing a correction to change the remaining work while
  passing the run's previous action evidence to the agent. When the provider cannot
  serve a complete snapshot, dispatch waits without consuming a retry attempt;
  cached messages cannot establish whether a new correction exists. A new follower can wake a failed
  head early, once per follower cursor; provider rate-limit resets are respected.
  Other threads and members proceed independently. Exhaustion terminalizes the
  failed trigger; unacknowledged followers remain available for a later run.
- A resumed session is continued only for the same run/event and input cursor it
  last worked on. A newer batch is delivered as new input; cursor regression,
  unorderable cursors, or a run/event identity mismatch rotate the session, record
  `continuation_rejected`, and re-feed historical context with the unread batch.
- An AI CLI turn stops after three hours (`TURN_TIMEOUT_SECONDS` in
  `intelligences/agent_runtime/models.py`), with the same deadline for every
  adapter. A `timeout` failure ends the run's completion attempts immediately.
  The ticket selector leaves one explanation on the originating ticket or PR,
  and autonomous runs announce the stop in chat. A later reply makes the work
  selectable again through the usual patrol rules. The next run reuses the
  work's healthy provider session and supplies its new instructions, including
  its own completion requirement. Session IDs learned during an unfinished turn
  are saved on timeout; the session store's normal rotation limits still apply.
  `member git prepare` retains a checkout while its work has uncommitted changes,
  unpublished history, a stash, an in-progress Git operation, special index flags,
  detached HEAD, or
  linked worktrees. Other work borrows an idle checkout of the same repository,
  keeping ignored build caches such as `target` and `node_modules`; only when
  none is available does it create another clone. After a successful prepare,
  excess idle clones are removed inside the guest, keeping at most one beside
  the retained checkouts. Before deletion, a slot's record moves to the host's
  discarded registry. Cleanup failures are retried from that registry even when
  the partial deletion has already removed Git metadata; those slot IDs cannot
  be allocated again until cleanup finishes.
  Publication is established from host-fetched references and successful pushes,
  with their objects retained on the host after squash merges and ref deletion.
  Guest remote-tracking references do not establish publication. Every idle
  checkout starts at the requested remote branch or current default, including
  after a force-push or branch deletion, and tracks that remote branch explicitly.
  The same unfinished work keeps its HEAD, index and files. Host-only atomic slot
  records name the repository and current work assignment. Every run calls prepare and
  uses the returned `repo_path` and `branch` rather than a remembered path, which
  may since have been reassigned. Member commit/push reject a registered slot
  owned by another work. An empty `branch` denotes retained detached HEAD.
  These files and provider sessions live on the device that ran the turn, so
  resumption uses that device.

  Older clone directories (`clones/<person>/<repository path>`, and the earlier
  per-work hash directories) and their old plaintext checkout records are unused.
  They may be removed manually after checking for uncommitted changes,
  unpublished commits and other unfinished Git state that must first be kept.
- Rate limits from AI CLI tools are detected in the isolated environment
  (`guest/cli_agent.py`), never amplified by in-process retries
  (`commands/agent_turn.py` raises them as they are), and recorded on the host by the
  ticket selector and chat selection through shared logic
  by the workflow dispatcher as a `workflow.rate_limited` diagnostics
  event. Ticket selection and
  the chat pending queue defer re-entry until the provider's exact reset timestamp
  when one is available. Ticket and Slack notices and trace summaries display
  that timestamp in the host machine's local time with its UTC offset (for
  example, `2026-10-03T06:30:12Z` becomes `2026-10-03 15:30:12+09:00` in Tokyo),
  ahead of the provider's wording. `utils/rate_limit_display.py` selects the
  display value and message variant for all three. Without a readable timestamp,
  they state that the reset time is unknown; timestamps without an offset also
  count as unknown. Any provider wording is identified as the message at detection
  time. A ticket with an unknown reset time waits for a new comment before it can
  be selected again; chat retries automatically while attempts remain. The notices
  explain these different retry rules. Display formatting does not change the raw
  timestamp or the retry policy.
- Diagnostics keep provider turn success, workflow completion evidence, and dispatch
  lifecycle as separate layers : a
  clean provider turn without a recorded completion raises
  `workflow.completion_missing`, and the dispatcher records
  `chat_dispatch.retry_scheduled` / `chat_dispatch.abandoned`.
  `retry_scheduled` requires an actual `chat_dispatch.retry_scheduled` event;
  completion evidence alone does not imply a retry is scheduled, since the ticket
  workflow shares the same completion layer but, when its attempt budget runs out,
  the ticket selector posts an error comment instead of dispatching a retry, so missing completion
  evidence without a dispatch event resolves to `incomplete`.
- Status derivation for one execution lives in `gb-observability` and
  nowhere else: the execution index summaries and the activity history sessions ask
  the same question about the same records, so they read one implementation. A
  trace becomes success only through an event the layer that opened it records
  (`TRACE_COMPLETED_EVENT_TYPES`). `span.finished` says a single provider call
  returned, and chat selection makes an LLM decision before its agent turn starts,
  so treating that span as the trace's success showed still-running executions as
  finished. Every route that opens a `trace_scope` therefore records its own
  completion event; tests at the execution boundary cover that
  population and fails on a new trace root until it declares which event ends it.
- A boundary that records the start of a run records its end however the run
  ends, cancellation included. Stopping the service cancels the work it drains
  and Ctrl-C ends an interactive member command, and neither
  `asyncio.CancelledError` nor `KeyboardInterrupt` is an `Exception`, so a
  boundary catching only `Exception` leaves `command.started` as the trace's
  last record and the execution reads as running forever. The same test
  enumerates the boundary functions and requires them to catch `BaseException`.
  How the failure is classified belongs to
  the host command failure path, because the
  `code` decides what the screens do with it: `cancelled` and
  `cli_agent_authentication` open no generic execution alert.

### Native agent runtime

`guest/*` holds the provider adapters behind a provider-neutral boundary for Codex
App Server and Claude Code stream-json, whose shared models are
`intelligences/agent_runtime/models.py`. It defines execution context, conversation key/record,
normalized events, terminal results, errors, explicit resume policy, process lifecycle,
and redacted diagnostics. Provider adapters own only protocol translation.

Logical conversations are keyed by person, adapter, work kind, and stable work identity
(ticket URL or Slack bot/channel/thread root), not by scheduler run. The work is the
host's to settle: the ticket selector and the chat selector fix it with its subject
when they select the trigger, an assistant of the screens names its conversation, and any other
command does its own run's manual work. The command's grant holds every turn and member
command to it, so nothing in the isolated environment names another subject or another
work's conversation. The versioned
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

**Paths between the host and the isolated agent environment** are decided by
`crates/gb-environment/src/contract.rs`. A host directory is mounted once at its
absolute host path. The guest has its own home, `/opt/guildbotics/home`. Granted
directories under the host home also have links at the corresponding guest home
paths, so `~/<grant>` work inside the microVM. When
grants nest, only the outermost gets a link. `guest_path()` rejects host paths
that overlap `/opt/guildbotics` or `/run/guildbotics`. Report real paths to users.

The command can reach its working directory, explicit grants, and declared mounts.
Background runs and screen runs without cwd use machine-owned `run/files/<run_id>/work`,
mounted only into that run. Read-only commands retain their empty guest cwd.
Owner-authenticated routes list and download completed run files using directory
capabilities without following links; only regular files are served. Files and
independent upload-ID directories expire after seven days and are pruned on the
next use. Run requests select their read-only uploads explicitly.

`gb-base/src/host_path.rs` and the environment contract validate bind sources,
including ancestry, symlinks and protected paths. The contract protects device
state, registered workspace state and credential locations, including paths
that do not exist yet. Workspace admission checks grants before registering a workspace. Explicit host-owned mounts such as a
member clone use their own access rules.

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
understands only the common `model` key; every other key belongs to the adapter.
A level with no mapping is a warning, never a failure — mapping coverage differs
per provider, and halting a run over a missing overlay would cost more than
running without it.

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

`default` and unspecified both request no effort overlay. A settings change
does not itself rotate a native session. Claude sends `--model` and `--effort` with
`--resume`; Grok confirms `model` and `reasoning_effort` through ACP
`session/set_config_option` after `session/new` or `session/resume`. Claude
re-sends the last recorded value for an omitted setting on resume. Grok keeps
the setting returned by its resumed session when no new value is requested.
Codex sends its settings on every `turn/start`.

Effort decisions are recorded through a safe allowlist — requested level,
resolved level, effective model id, the *names* of the applied parameters, and
an unsupported flag — never the effective parameter values, which can sit
alongside API keys and headers. The effective model id and the effort level are
the two deliberate exceptions to "names only": both are values, and both are
written. Trace / diagnostics detail shows both values. The member's CLI tool
cards and activity member column also show the last turn's model on this device.
`GET /intelligences/cli-agents/last-turns` returns that reading per member and
tool from the retained diagnostics index, including failed turns and turns with
no reported model. CLI span summaries record `agent.adapter` and
`payload.model_specified` alongside `agent.slot` and `payload.model`.
The brain determines `model_specified` from the model sent to the turn after
slot settings and effort overlays are resolved; changing settings later cannot
change the recorded classification. Rows without those facts are not inferred
from current settings.

What a turn *really* ran on is decided by the layer that holds the provider
knowledge and is carried out on the normal result path: each adapter fills
`AgentTerminalResult.model` / `.effort` with the value its provider reported
(claude's `system/init` model, copilot's confirmed session options, grok's
confirmed session options or `_x.ai/models/update.currentModelId`) or with the
one it imposed itself
(codex's validated `turn/start` settings and the catalog's advertised default
model and `defaultReasoningEffort`, claude's `--effort` flag, `agy`'s command
line), and the brain passes
them to `record_span_summary()` and logs one line per span. The conversation
record retains the last reported or imposed values (`effective_model` /
`effective_effort`, cleared on rotation). Claude re-sends recorded settings on
resume; Grok reports the values its session confirms. A turn that reports no
value retains the last known value in the record. An omitted Codex setting on a
resumed thread is reported from that record without independently verifying
that the provider retained it. A provider that reports nothing and was given
nothing leaves both empty rather than inventing an effective value; the span's
model then stays empty
too, while the `agent.slot` / `model.slot` attribute always names the slot so
traces stay searchable whatever model the slot resolved to.

## 5. Command Execution Framework

`gb-server/src/commands.rs` prepares the main command and runs it in the
microVM. `gb-config` resolves the member and command, reads declarations without
executing the file, and builds the command table used inside the guest. The
guest entry builds the Python `Context`; `commands/runner.py` runs child
commands in order and then the main command. `Context.pipe` carries text and
`Context.shared_state` carries structured values between commands. Changing the
order in which they are updated changes workflow behavior.

Commands can be Markdown prompts, Python, shell, YAML, or inline operations
such as `print`, `to_html` and `to_pdf`. File precedence is defined in
`gb-config/src/commands.rs` and used for both the command picker and execution.
Extension order comes first, followed by language and location precedence.
Within the applicable candidates, the selected language falls back to English
then to a name without a language suffix, and a member can override a shared
command. A run without an explicit member uses the project default or the first
active agent. The screen uses the resolved default the API returns.

- Editing shared commands from the screens goes through the Local API, never
  the filesystem plugin. `gb-server/src/command_files_api.rs` owns the editable shared root,
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
- The command editor's AI assistant separates conversation from mutation.
  Each read-only `/commands/author` request sends the complete current editor buffer,
  latest instruction, permitted operations and effective shared command sources.
  A question returns `action=answer` with no changes and bypasses source validation;
  an explicit modification request returns a reviewed `create`/`update` proposal.
  Existing-file authoring can update only the current command without renaming or
  changing its format, but may create new shared helper/wrapper commands. The screen
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
`gb-member/src/memory.rs`; audit in `gb-member/src/audit.rs`).

- **Document**: 1 document = 1 directory (`meta.yml` + `body.md`) under
  `<workspace-data-root>/documents/`. `meta.yml` holds `schema_version`,
  title/summary/keywords, typed `source` entries (ticket/PR/channel/thread URLs),
  creation/update timestamps and member IDs (`created_at`/`created_by`,
  `updated_at`/`updated_by`), `pinned`, and `kind`. Both files stay under the memory file size limit
  (`MAX_MEMORY_FILE_BYTES`), checked together before either is written, so a document is
  never half old and half new.
- **Scopes**: `personal/<person_id>/` and `team/`. `memory promote` moves a document
  from personal to team.
- **Operations** (`guildbotics member memory ...`): `record`, `recall` (lexical grep
  over meta+body; `--meta-only` for source-URL pinpointing), `get`, `update`,
  `touch` ("this note actually helped" — recency only), `archive`, `promote`.
- **Recency (MRU)**: the device keeps its own record,
  `documents/recency/<device_id>.json`, of when each member last used each document
  (`record`/`update`/`touch`/`promote`; read operations do not count). The record is
  written inside the operation's shared-write span, so it is not lost when the
  best-effort audit journal fails or trims. The digest takes each document's latest
  use across the records and leaves out archived or missing documents.
  `member context` output includes a `memory` block: `digest` (top-N recent metas —
  hints that a relevant note exists) and `pinned` (always-on documents, body
  included).
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
(host resolution: `gb-workspace/src/selection.rs`). The workspace root is never inferred from
the process cwd or a member working clone.

| Root               | Location                                                                                         | Holds                                                                                                                                                          |
| ------------------ | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Machine state root | `$HOME/.guildbotics/data` (fixed)                                                                | `active-workspace.json`, `device.json` (this device's ID and display name), `run/app-api.lock`, `run/service.lock` — state needed _before_ a workspace is chosen                  |
| Identity root      | `$HOME/.guildbotics/identity` (`GUILDBOTICS_IDENTITY_ROOT` inside a command's environment)      | The members' identities, shared by every workspace, laid out like a workspace root: `config/team/members/<person_id>/identity.yml` and the avatar beside it, `config/secrets.yml` (the index of the store of members' credentials and connection Secrets such as `BRANVIL_ADMIN_TOKEN`), `config/connections/branvil.yml` |
| Workspace root     | `--workspace`, `GUILDBOTICS_WORKSPACE_ROOT`, or the persisted active workspace                   | GuildBotics-only directory. `.guildbotics/config`, `.guildbotics/state`, `.guildbotics/local` live here                                                        |
| Config             | `<workspace>/.guildbotics/config`                                                                | project YAML, each member's participation (`team/members/<person_id>/member.yml`), `secrets.yml` (key names), transcript settings                                                                 |
| Workspace state    | `<workspace>/.guildbotics/state`                                                                 | memory documents, chat control state, task-run records, interactive session records, activity events                                                          |
| Local state        | `<workspace>/.guildbotics/local`                                                                 | diagnostics, transcripts, `secrets.json` (this machine's keychain namespace), person leases, chat message cache, member clones and the host's own repositories member git pushes from (`member_git/`), AI CLI sessions, work dirs, `debug.env` |

Invariants:

- The machine root is `GUILDBOTICS_HOME` or `~/.guildbotics`, independent of workspace selection.
- A member is split in two files. Its identity (`person_id`, `name`, `person_type`,
  `account_info`, `profile.character`, `speaking_style`, `relationships`) is the same in
  every workspace and lives under the identity root; its participation in a workspace
  (`is_active`, `profile.roles`, `message_channels`, `routine_commands`,
  `task_schedules`) and its `intelligences/` and `commands/` overrides live in that
  workspace (`IDENTITY_FIELDS` / `PARTICIPATION_FIELDS` in `entities/team.py`). Team
  loading refuses a field in the other file's place and leaves out a workspace
  member whose identity is missing. A `person_id` never changes.
- Workspace root is resolved only from `--workspace`, `GUILDBOTICS_WORKSPACE_ROOT`,
  `GUILDBOTICS_CONFIG_DIR` when it is `<ws>/.guildbotics/config`, or
  `active-workspace.json`. Missing workspace is an error for writes.
- `config/` and `state/` are the workspace's shared files, written only through the
  helpers of section 8; `local/` is this machine's own. Anything
  whose value is decided by the machine rather than the workspace belongs in `local/` —
  absolute paths, for example.
- `GUILDBOTICS_WORKSPACE_ROOT` is published at the workspace application boundary.
  Agent execution copies that resolved value into `AgentExecutionContext`.
- There is no `GUILDBOTICS_DATA_DIR` override and no workspace `.env` file.

## 8. Shared State Writes

GuildBotics runs on one machine. Its workspace's `config/` and `state/`, and the identity
root's `config/`, are still written by several writers that do not know about each other:
the scheduler's member threads, the API handlers, the member commands the backend runs in
process, and processes of their own such as a local `guildbotics run` or
`guildbotics secrets`. `local/` is this machine's own state and is not among them.

**Identity.** A workspace and a device are each identified by a UUIDv7 minted once
(`gb-workspace/src/identity.rs`). The workspace ID is in `state/workspace.json`: the member CLI
names the workspace it means by it, so a backend serving another selection refuses rather
than acting there. The device ID and its display name (the host name) are in
`~/.guildbotics/data/device.json`; records carry the device ID, and the owner's Branvil git token
is labelled with it. Neither identity authenticates anything. Shared
records derive from `SharedRecord` (`gb-workspace/src/identity.rs`), which rejects unknown
fields, so machine-local data is kept out of them by structure rather than by a list of
forbidden field names.

**Shared writes.** In `gb-base/src/shared_writes.rs`, `SharedWrites` provides a
`Writer` for a span protected by an OS file lock. A read-modify-write starts
before the read. Renames, deletes and multi-file operations use the same
boundary. When both identity and workspace are involved, acquire the
identity lock first. Paths under `local/` are device-local. Shared JSON uses
sorted keys and a trailing newline.

**Config revisions.** `gb-config/src/repository.rs` gives the settings
screens a revision for each file they read. A save may name the revision it
expects; if it is stale, the save is refused. A directory-wide edit also
compares the set of paths it read. The config write API compares revisions,
applies the write and reports the resulting revisions under the same lock.
Memory and conversation writers use the shared lock without config revisions.

**Record schema.** Shared state carries its schema version. Check the
[record formats](../records/README.md) when changing a writer or a reader.
`config/secrets.yml` contains the key-name index.

**Task runs.** The common execution boundary (`gb-server/src/execution.rs`) records each run's
start and terminal state in `state/task-runs/<run_id>/result.json`. Accepting an input
scans the member's runs and creates the record under the shared-write lock, so two workers
cannot both accept the same event or schedule slot, and a running or completed input is not
accepted again after a restart. An input is accepted again only when its run ended proving
the work did not happen: it raised or was cancelled. That next attempt continues under the
same run id, keeping the evidence earlier attempts recorded. A run whose outcome could not
be observed (`result_unknown`) keeps its input, because the work may already have taken
effect; only the user starts it again.

An autonomous operation that could not connect to its service ends as `deferred`, through
the port's `ServiceUnavailable` type. This terminal state releases its input for a later
attempt and appears as information in Activity. Its command boundary records
`command.deferred` rather than a failure; the scheduler preserves its error count and
continues other work; a deferred scheduled occurrence stays due. Manual callers and failures
after a request may have arrived retain their usual failure behavior. Every run write stamps
the current schema, including updates to an existing record.

A run record means work was taken. The ticket patrol selects its ticket outside the boundary
and claims a slot only for a ticket it dispatches, so an idle patrol leaves no record (and no
trace). A chat event is different: whether it holds work for the member is known only after
the thread has been read. The dispatcher selects it before opening a trace, so an event that
is not work for the member (an edit, its own message, one its participation excludes) leaves
neither a trace nor a record. For an event that is, it opens the trace, claims only the
event's identity (`record_start=False`), and selection records the start itself before
judging the batch; the boundary finishes only a run that started. The boundary sits inside
the work it tracks on every route, so a failure or a forced stop reaches it and the run ends
`failed` or `cancelled`, not `succeeded`. The run is its trace (`run_id == trace_id`, for the
ticket workflow too), and the boundary mirrors the trace's source and attributes into the
record at start and at finish, so the record names the run's PR / issue or chat thread
without the trace.

## 9. Secret Storage (SecretStore)

```sh
guildbotics secrets set OPENAI_API_KEY
guildbotics secrets --identities list
```

The first command prompts for a workspace key. The second lists the names of the
keys shared by all workspaces, including member credentials and the Branvil
connection. A workspace store refuses member credential keys. The member key
prefix uppercases the member ID, changes hyphens to underscores and appends
`_`. It must be unique across the identity store.

`gb-config/src/secrets.rs` stores values in `.guildbotics/local/secrets.enc.json`.
The OS keychain holds the machine key. `config/secrets.yml` lists only key
names and `local/secrets.json` holds the store ID. An unreadable or invalid
index stops the operation so a bad read cannot discard recorded keys.

For a machine move, export and import with `guildbotics secrets` in the target
workspace and with `--identities` for shared credentials. The dotenv file is a
transfer format. Dollar expressions are literal and key-only lines are
rejected before saving. Real environment variables can supply configured keys;
a workspace `.env` is never read at startup. Tests inject a temporary keychain.

Branvil keeps only the owner's token in its data directory's `secrets/`.
Members' upstream credentials and the webhook URL are in the identity store
under lower-case `branvil_*` names (`gb-server/src/branvil_vault.rs`), so
`--identities` export and import carry them too. See Branvil's
[backup and restore guide](../crates/branvil/docs/backup-restore.md) for what a
backup includes.

## 10. Observability and Diagnostics

`guildbotics/observability/` is the recording substrate (depends only on `utils`):

- **Correlation**: `trace_scope` / `span_scope` / `correlation_fields` /
  `set_attributes` correlate everything that happens in one execution — a manual
  command, one scheduler cycle, one batch of unread chat messages — under a single `trace_id`,
  with spans for LLM / AI CLI tool calls and `call_id` correlation.
  Trace attributes carry structured search keys (e.g. `target.url`, `target.label`).
  Work targets use `target.provider/kind/url/label/title/action`; chats use
  `chat.provider/channel/thread/message`. Fact event types name their subject:
  `repository.push`, `pull_request.item`, `ticket.item`, `ticket.comment`,
  `target.recorded`, and `target.scope_refused`.
  Idle ticket patrols (a routine cycle whose selector finds no actionable ticket) open
  no trace and leave no records; liveness is exposed instead as a per-member patrol
  heartbeat (`member_routines`) in the scheduler's runtime status.
- **Persistence**: `run/diagnostics.jsonl` is a small searchable index containing session
  pointers, execution boundaries, span summaries, and domain events. Complete events, logs,
  spans, and request/response I/O are stored as one JSONL transcript per execution or system
  session under `run/sessions/` (`gb-observability/src/streams.rs`). `standard` detail omits high-volume
  thinking/delta events and bounds AI CLI tool stderr to its final 8 KiB; `full` keeps them.
  The output a top-level command run returns to its caller is an I/O record of its
  trace (`command.output`, written by `run_main_command`), so the Edit Command screen
  shows a run's output from the server, also after the run ended while no screen was
  open.
  Session transcripts default to 30-day retention. Memory audit remains a separate store
  under `documents/memory_events/<device_id>.jsonl`: the device appends to and trims its
  own bounded journal, and readers merge the journals in timestamp order.
- **Lifecycle records, fact events**: one execution is one shared record. A workflow
  run is its task-run record (`state/task-runs/<trace_id>/result.json`); an interactive
  member CLI session is its session record (`state/sessions/<trace_id>.json`,
  `gb-observability/src/interactive.rs`), rewritten at the end of every command with the session's
  first command, its last command's result, and the first target attributes its
  commands recorded. The shared activity events (`gb-observability/src/activity.rs`) carry only
  the facts recorded inside an execution — provider domain outcomes, completion
  evidence, dispatch decisions — never the `command.*` /
  `member.command.*` boundary events, which stay in local diagnostics. The Activity
  History therefore reads one record per execution instead of folding every command
  it ran. Two consequences are
  deliberate: an interactive session's status is its last command's result rather than
  "failed once, failed forever", and an `ERROR` log no longer marks a session failed
  (logs are local; the diagnostics index still folds them for its own list).
- **Consumption**. `gb-server/src/activity.rs` reads lifecycle records, fact
  events, memory audit entries and observed targets to serve Activity. It
  normalizes display payloads before sending them to the browser. The
  execution list and timeline use the titles and statuses derived by
  `gb-observability`. A read can name a trace's target without becoming an
  activity link. Manual commands remain visible in Diagnostics, and any
  writes they make still appear as fact events.
- **System health alerts**: successful and failed verify/scenario-diagnostics runs are
  recorded as structured diagnostics events. `gb-server/src/intelligences.rs` folds those
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
  workflow-time failure to become critical without waiting for diagnostics, and record
  `credential.verified` when a later turn of the same member is answered, which closes
  that alert without a diagnostics run. Inference with a workspace API key (an LLM
  provider's through agno, Jev's) is recorded where the call is made,
  the guest’s inference implementation, with the host accepting bounded summaries through `gb-server/src/host_window.rs`. Every such call ends its span naming the key
  (`credential.provider` `llm` / `jev`, `llm.provider`) and, when refused, why
  (`error.category`: `authentication`, `credit`, `rate_limit`, `other`, decided by
  the status and error type the provider reported in `llm_providers.classify_failure`),
  with the status and what the provider answered (`error.status_code`,
  `error.response`). The answer is recorded with the key the call used and the
  workspace's secrets masked and bounded (`redact_for_sharing()`), because the
  records are mounted into the environment of a command that inspects
  diagnostics; it never crosses the command window, which carries only the
  failure's kind, status, and category.
  The span is the record: the alert is one per provider account
  (`inference:llm:<provider>`, `inference:jev:`), opened by an `authentication` or
  `credit` refusal and closed by the next call of the same key that succeeds; a rate
  limit passes by itself and opens none. It is keyed apart from the verify and
  diagnostics checks of whether the key is configured (`credential:llm:<provider>`),
  which say nothing of whether the provider takes it and so never settle a refusal;
  the converse holds, so a call that succeeds also closes the key's missing-key alert. The setup screen shows each key's latest
  refusal, of any category, from the same spans (`/intelligences/inference-failures`).
  The desktop
  polls `/system-alerts` and renders the result above every route with links to
  diagnostics, setup, service state, or the correlated trace. Provider classification
  remains in the backend; the frontend only translates stable alert codes.

- **Read-only diagnostics queries**: `guildbotics diagnostics traces | trace <id> | system`
  (`crates/guildbotics/src/diagnostics_cli.rs`) reads the index and transcripts through
  `DiagnosticsStore` and emits raw records as JSON. It is a top-level group rather than
  a `member` subgroup because a diagnostics query is workspace-scoped, not member-scoped:
  it needs no acting member, takes no execution lease, and `--person` is a filter over the
  records' `person_id`, not an identity. It never calls `start_maintenance()`, so reading
  cannot rotate or prune what it reads, and it carries no display normalization — record
  meaning is taught to its consumers instead. `--query` searches each candidate's full
  transcript, not just the index, because the text a failure describes itself with lives
  in log and I/O records that the index never stores. The assistants' traces are excluded by
  default so the troubleshooting agent does not investigate its own conversations.

## 11. Screens and Server

`desktop/` is a React (Mantine, TanStack Query) frontend that the server serves to a
browser; the repository is a monorepo on purpose. The server runs from a checkout on the
Hub: `guildbotics server start` in a terminal, or on macOS the LaunchAgent that
[`scripts/hub-server.sh`](hub-server.md) installs. The LaunchAgent runs a signed copy of the locally built Rust executable, at one fixed path so that macOS keeps the access it granted, with Branvil in the same process.

- **Local API boundary**: the browser calls the Rust backend (`gb-server`), which serves both the API under `/-/guildbotics/api` (REST,
  and the `/-/guildbotics/api/events` WebSocket) and the screens themselves (the Vite build of
  `desktop/`: `/-/guildbotics/`, `/-/guildbotics/assets/*`, and `/-/guildbotics/-/enter` and `/-/guildbotics/-/authorize`, which
  the main page tells apart by path; `gb-server/src/ui.rs`). It listens on loopback only, on
  one fixed port (`GUILDBOTICS_SERVER_PORT`, 7878 by default), which every program of
  the machine reads from the environment: the server, its CLI clients, and the Vite dev
  server that proxies `/-/guildbotics/api` to it. GuildBotics runs
  on one machine, the Hub; other machines reach it from a browser through SSH forwarding
  to the same port (`scripts/hub-forward.sh` / `.ps1`, plain OpenSSH user services).
- **Authentication** (`gb-server/src/auth.rs`, as Branvil's "Entering the GUI"): there is one
  person, the owner, and no sign-in screen.
  - The server keeps its records in `~/.guildbotics/data/server/<port>/` (`0700`):
    `owner.token` (`0600`) and `auth.json` (tokens and sessions, by SHA-256 only). A
    token is `gbt_<id>_<secret>`. Whoever can read `owner.token` is the owner. At every
    start the server checks that the file still authenticates; when it is missing or
    does not, it revokes the previous file token, ends every session, and writes a new
    one. Only the process that bound the port opens these records, so a second
    backend whose bind fails changes nothing.
  - A client holding an owner token (`Authorization: Bearer`) asks
    `POST /-/guildbotics/api/session/links` for an entry link, `{base}/-/guildbotics/-/enter#<code>` (32 random
    bytes, single use, 60 s, in memory as its hash; `?next=` names a page of this
    server). The code is in the fragment, so it is in no request line, log, or
    `Referer`. The page removes it from the address bar and exchanges it at
    `POST /-/guildbotics/api/session` for the session cookie `guildbotics_session_<port>` (`HttpOnly`,
    `SameSite=Strict`, 30 days; the port is in the name because cookies are not
    separated by port, which also keeps it apart from Branvil's `branvil_session`) and
    its CSRF value. As in Branvil, a session ends after its 30 days however much it is
    used, and the screen then says how to enter again. Every non-GET
    request made with the cookie must carry `X-GuildBotics-CSRF`, else
    `403 csrf_mismatch`. `guildbotics open [--print]` is that client on the command
    line.
  - A machine that cannot read `owner.token` gets an owner token of its own through the
    consent screen: `guildbotics connect` listens on a loopback port and opens
    `/-/guildbotics/-/authorize` with its name, that `redirect_uri`, a `state`, and a PKCE `S256`
    challenge; the owner allows it with a session (`POST /-/guildbotics/api/apps/authorizations`), and
    the CLI exchanges the single-use, 60-second code bound to its name, address, and
    challenge at `POST /-/guildbotics/api/apps/token` for a token labelled `app:<client>`, replacing
    that client's previous one. The token is kept in the machine's OS keychain
    (service `guildbotics-server`). `guildbotics open` offers `owner.token`, then that
    token, and uses the first the server proves it knows (`gb-server/src/client`):
    an owner file outlives its server, and the port may lead to the Hub by then. `run`
    delegation hands over this machine's paths and secrets, so it offers `owner.token`
    alone: only the server running on this machine knows it.
  - The guard runs before every route, the screens included: `Host` must be
    `127.0.0.1`, `localhost`, or `[::1]` with the port (else `421 misdirected`, against
    DNS rebinding), and an `Origin` must be that same origin (else
    `403 origin_mismatch`). No CORS header is ever sent. Every response carries
    `Content-Security-Policy` (`script-src 'self'`, `form-action 'self'`,
    `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff`, and
    `Referrer-Policy: no-referrer`.
    The WebSocket needs the session cookie and the server's own `Origin`.
  - Unauthenticated are only the proof below, the two code exchanges, the screens
    (which hold no data), and the provider pages a browser comes back to from another
    site, which act on their unguessable `state`.
- **Who receives a token**: a loopback port is not owned by an OS user, so a token goes
  only to a process that showed it holds the port or knows the token.
  - The server binds the fixed port (`GUILDBOTICS_SERVER_PORT`, 7878 by default),
    listens, writes its owner token file, and then announces the port as the only line
    its stdout ever carries (`GUILDBOTICS_APP_API_PORT=<port>`); afterwards stdout is
    stderr. A launcher that waits for the line (the E2E harness, for example) addresses
    the port only once its own child holds it. The server announces before loading the
    workspace, whose secrets can wait on a keychain prompt.
  - Host clients (`guildbotics run` delegation and `guildbotics open`) go through `gb-server/src/client`. For each token it
    offers, it first posts the token's id and a fresh 32-byte nonce to
    `/-/guildbotics/api/local-api/proof`, which takes no
    token and answers `HMAC-SHA256(sha256(secret), "guildbotics-local-api-proof\n" +
    nonce + "\n" + id)`. Only a matching proof attaches the token; a process that took
    the port while the server was down knows no digest, so it, a replayed answer, a
    malformed answer, or a timeout gets neither the token nor anything it guards. A
    health answer is no evidence.
  - Remaining limits: a port the genuine server leaves after a client's proof can be
    taken before the client's next connection (later requests usually reuse the proved
    connection). A browser redirected back by Branvil's consent reaches the port even after the server exited, carrying a one-time code.
- **Runtime lifecycle**: `guildbotics server start` runs one foreground process.
  It owns one listener and `app-api.lock` until both services exit. Branvil's
  `prepare_embedded` returns a router and data/shutdown handles without binding;
  its routes return 503 until recovery completes. GuildBotics routes take
  precedence over Branvil's GUI fallback. `guildbotics server stop` drains
  GuildBotics' work and Branvil's CI jobs together. Branvil gives out no more jobs from the start
  of the stop. The screens and the CLI stay available meanwhile: the API refuses
  only the routes that would start more work for the stop to wait for (`api::REFUSED_WHILE_STOPPING`:
  runs, the service, environment builds, usage probes, workspace switches) with
  `503 server_stopping`. Interactive member operations are answered until the
  drain ends, and the event streams stay open until the listener closes. Events close before the shared
  listener's request grace and repository-operation drain. `--force` and SIGTERM skip the wait. The
  LaunchAgent runs the server directly and uses the CLI's forced-stop
  timeout for SIGTERM. The workspace's service settings control whether patrol and chat
  reception start with the server.
- **Installing the CLI and the skill**: `guildbotics install` copies the
  running executable to `~/.guildbotics/bin/guildbotics` and adds a link under
  `~/.local/bin` when the path is available. It copies
  `skills/guildbotics/SKILL.md` to detected tools' user skill directories.
  The recorded content hash lets an update preserve a skill the user edited.
  The Hub script runs this command after the selected build starts.
- **This machine**: the settings section for what belongs to the machine rather than to a
  workspace — the registered workspaces (`/device/workspaces`) — listed in the settings nav
  under its own "This machine" group and reachable before any workspace is selected.
- **Config saves are explicit and revision-checked**: every settings screen saves on a
  button, and sends back the revisions its read reported. A refused save reloads the screen
  instead of resending, since resending is exactly the overwrite that was refused.
- **AI assistants**: the command editor and the diagnostics screen each host a
  conversational assistant. Each is a bundled command (`assistants/author_command`,
  `assistants/troubleshoot`) that runs through the same path as any command the screens
  run (`AppRuntime._execute_command` → `prepare_command` → `run_main_command`): once
  the run is accepted for the workspace, the API resolves the command exactly once,
  decides its slot and tracking on that runner, builds the command's JSON input from
  what it declares, and runs that same runner under a fresh trace labelled
  `author:<command>` / `troubleshoot:<trace_id|view>`, and reads the typed model the
  command returns. The API names the assistant's conversation as the run's work
  (`PreparedCommand.work`), so every turn of it resumes the provider's own session. Both declare themselves read-only (`read_only:
  true`), and a read-only command is confined by its isolated environment, not by its
  prompt or its provider: the whole run, every turn included, inherits the declaration, the
  environment spec mounts every host directory read-only with an empty working
  directory and lets the network reach only the provider's API and the member broker,
  and the run takes no member execution lease, no manual-command slot and is tracked
  non-exclusively, though it still keeps the workspace from switching until it ends.
  Its native adapters belong to its own run, so it never closes another run's turn.
  So it stays usable while another command or that member's scheduled
  work runs — which is exactly when its logs are worth asking about. Command authoring
  can inspect the effective shared command sources and return structured proposals,
  which the command validates and sends back once for correction; only the separate
  explicit apply endpoint writes them. On the frontend,
  `desktop/src/assistant/` owns the shared chat panel and conversation state; each screen
  keeps its own mutation. Conversations are never persisted on this side.
- **Troubleshooting assistant**: opened from the diagnostics screen, it answers questions
  about recorded executions. It is given only the question, the focused view and where to
  look; it gathers its own evidence by reading the recorded runs and the workspace
  configuration, which its command declares it inspects (`inspects`) and its environment
  mounts read-only. This keeps
  prompts small and lets it follow leads into other executions and into the commands and
  settings they ran with. What it reads
  is untrusted — logs carry Branvil ticket bodies, chat messages and external tool output —
  which is why the read-only confinement above is enforced rather than requested. It returns
  the answer plus the trace ids it read, and the backend drops any id that was never
  recorded before the frontend turns it into a link. The record detail and the assistant
  share one mutually exclusive right-edge panel, so two drawers never fight over the same
  strip; the assistant is non-modal, so the executions list behind it stays clickable.
- **Support targets**: the service script targets an Apple Silicon macOS Hub. CI runs on Linux. Other machines reach the Hub through OpenSSH forwarding.

See `desktop/README.md` for build, development, and test instructions of the screens.

## 12. Domain Model Notes

Core entities live in `guildbotics/entities/` (`Team`, `Person`, `Task`, `Message`).
Two Person distinctions matter architecturally:

- **AI members** are execution subjects: active members get a scheduler worker, Slack
  subscriptions, credentials, and an intelligence (brain) configuration.
- **Human members** are _references to real people_ (roles, Slack user id, Branvil
  username, avatar) used for handoff candidates and assignee mapping. They are saved
  with `is_active: false`, are never scheduled, hold no bot/app credentials, and the
  desktop settings UI intentionally hides all agent-execution fields for them. Treat
  any path that would execute a human member as a boundary bug, not a feature. The
  host rejects human members when resolving an execution subject, before running
  a command or acting as a member. Member help and task status do not need an
  execution subject.

## 13. Extension Points

- **New brain**: implement `Brain`, register via the intelligence mappings
  (`guildbotics/templates/intelligences/*.yml`).
- **New AI CLI tool**: implement a native adapter under
  `guest/`, register it in `guest/factory.py`, and add
  its catalog entry to `CLI_AGENTS` in `intelligences/cli_agents.py` with a matching
  `templates/intelligences/cli_agents/<tool>/default.yml`. There is no YAML-only path:
  a tool without an adapter cannot run. Declare in its catalog entry how the tool is
  provisioned into the isolated agent environment (`CliAgentProvision`: the pinned
  npm package, its state root, which entries persist, its login command, its API
  domains); the access contract (`gb-environment/src/contract.rs`) is
  enforced by the environment (`environment/`), not translated per
  adapter.
- **New command type**: subclass `CommandBase` with `extensions` / `inline_key`; the
  registry picks it up (`commands/registry.py`).
- **New integration**: implement the relevant `gb-ports` traits and register
  the provider in `gb-integrations/src/factory.rs`. Keep payload conversion,
  credentials and the provider's write checks in that implementation. Add setup
  support in `gb-server/src/setup_providers/` when the provider is configurable
  from the screens. Verify the same operations used by the other providers and
  the explicit unsupported results for optional operations.

- **New activity event/link kind**: add the recording payload (capability →
  observability), the normalizer/API model (`gb-server`), and the frontend rendering
  (desktop) as three separate responsibilities.

## 14. Related Documents

- `AGENTS.md` — working rules for this repository: responsibility boundaries, CI
  commands, testing strategy, prompt layer model.
- `README.md` / `README.ja.md` — user-facing setup and usage.
- `docs/custom_command_guide.en.md` / `.ja.md` — custom command development guide.
- `desktop/README.md` — desktop build/dev/test guide.
