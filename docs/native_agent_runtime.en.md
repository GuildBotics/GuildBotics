# Native Agent Runtime

GuildBotics uses native protocol adapters for Codex and Claude Code. Codex is driven
through [Codex App Server](https://developers.openai.com/codex/app-server); Claude Code
is driven with its documented `stream-json` input/output and an exact `--resume`
session id. Antigravity and GitHub Copilot remain one-shot YAML script adapters.

## Configuration

Select a native provider directly in `intelligences/cli_agent_mapping.yml`:

```yaml
default: codex
codex: codex
claude: claude
```

Native Codex and Claude definitions do not use files under
`intelligences/cli_agents/`. The only user-configurable native runtime boundary is
the Codex filesystem scope in
`intelligences/native_agent_policy.yml`:

```yaml
codex:
  filesystem_access: workspace
```

New workspace setup copies this file from the packaged template. Existing workspaces
that do not have the file continue to use that template until the policy is saved.
Configure the team policy in Desktop under **LLM / AI CLI tools → Advanced → Native
agent execution policy**. A member can inherit it or save an override from the
member's **LLM / AI CLI tools** tab; the override is stored at
`team/members/<person_id>/intelligences/native_agent_policy.yml`.

For headless operation, edit the YAML directly. `filesystem_access` accepts
`workspace` (the default) or `host`. Workspace access maps to Codex workspace-write
with network enabled; host access removes the Codex filesystem sandbox. Codex always
uses the non-interactive `never` approval policy, and any unexpected approval request
is declined. These fixed settings are not exposed as user choices.

Claude Code always runs non-interactively with `bypassPermissions`, preserving the
previous `--dangerously-skip-permissions` behavior. GuildBotics also passes a
session-level `sandbox.enabled=false` override because the Bash sandbox is not
compatible with the full range of ticket and chat workflow commands. A higher-priority
Claude managed policy remains authoritative. Claude permission and sandbox settings
are not stored in the workspace policy or exposed in Desktop.

The effective policy and every approval decision are written as provider-neutral
diagnostics events. Codex host access and Claude `bypassPermissions` can modify files
outside the workspace. Use them only with the documented credential isolation and in
an environment whose host access is acceptable. Invalid types, removed keys, and
unknown values fail validation instead of silently changing the effective boundary.

## Authentication

Install and authenticate each selected CLI before starting GuildBotics. Use the
provider's normal interactive login (`codex login` or `claude auth login`) as the same
OS user that runs the GuildBotics service. Provider credentials stay in the provider's
own credential store. GuildBotics does not copy them into its conversation store or
diagnostics.

GitHub, Git, and SSH write credentials are deliberately removed from native
agent process environments. The agent performs member-side writes only through a
validated `guildbotics member ...` command. Those nested commands inherit a short-lived
execution delegation identity, not a provider token.

## Exact conversation identity and resume

A logical conversation is keyed by `person + adapter + work kind + stable work
identity`:

- Ticket: the canonical issue or pull-request URL. Only completion retries within one
  workflow run resume the exact provider session. A later dispatch starts a fresh
  generation even for the same ticket.
- Slack: `slack:<bot-user-id>:<channel-id>:<thread-root-ts>`. Later messages in the same
  thread resume the exact session and advance the context cursor only after a terminal
  success.
- Manual: the explicit work identity supplied by the caller.

### Slack thread context delivery

The chat workflow passes the latest event and a bounded thread snapshot to the runtime
as separate values. The runtime selects the effective input based on the AI CLI tool's
conversation capability:

- Healthy Codex / Claude resume: only the latest event is added to the context already
  held by the provider session. The workflow may refresh a bounded snapshot for safe
  future rotation, but that snapshot is not injected into the healthy session.
- New or rotated native session: the bounded snapshot before the event and the latest
  event are injected exactly once.
- Conversation-less one-shot scripts such as GitHub Copilot: the bounded snapshot and
  latest event are injected on every invocation.
- A one-shot script such as Antigravity that resumes an exact conversation only within
  one dispatch declares `conversation_scope: dispatch`. Once its saved conversation ID
  exists, a completion retry receives only a continuation instruction and does not
  receive the same event again.

If a bounded snapshot cannot be built safely from the live Slack API, only a new or
rotated session or a non-resuming one-shot invocation uses the `inspect_required`
fallback. A healthy native resume continues from its provider session and the latest
event, so that fallback never causes a full-history duplicate. The cursor is persisted
only after provider terminal success, preserving an unprocessed event after a failed
turn. A completion retry with the same cursor is delivered as a continuation.

Records are atomically stored under
`<workspace-data-root>/agent-runtime/conversations/<person>/<adapter>/`. They contain
provider session/turn ids, cursor, usage counters, health, generation, and rotation
reason. They never contain provider credentials or raw protocol payloads.

GuildBotics never uses a provider's “latest” or implicit continuation mode. A missing
or unhealthy session fails exact `resume`; `auto` starts a new generation and rebuilds
context. Rotation also occurs after cancellation, malformed or incomplete streams,
process failure, provider context compaction, TTL/turn/usage limits, or a model change.
Codex `contextCompaction` and Claude `compact_boundary` events are normalized to the
same runtime event; the completed turn remains successful, while the next dispatch
starts a new generation and rebuilds the Slack snapshot.

Reset an exact logical conversation explicitly:

```bash
guildbotics member agent conversation reset \
  --person aiko --adapter codex --work-kind ticket \
  --work-identity https://github.com/GuildBotics/GuildBotics/issues/300
```

For Slack, pass the stable identity format shown above as `--work-identity`.

## Concurrency and shutdown

An OS advisory lease serializes all agent execution for one person across scheduler,
chat, manual API/CLI, and separate GuildBotics processes. Different people may run in
parallel. A nested member command is accepted only when its person, lease, delegation,
run id, live PID, and currently-held lock all match.

Native subprocesses start in their own process group. Cancellation, service shutdown,
protocol failure, and context close interrupt or terminate the group and reap the
owned process, preventing detached or zombie agent processes.

## Rate limits and diagnostics

Authentication and rate limits are classified from structured provider events. Claude
uses `system/api_retry`; Codex uses account/rate-limit RPC data. GuildBotics does not
parse human stderr text for these decisions.

When a reset timestamp is available, ticket selection and the chat pending queue defer
the next attempt until that exact time. They do not consume in-process completion
retries. Diagnostics use `agent_runtime.*`, `workflow.rate_limited`, and
`credential.failed` records correlated by person, run, logical conversation,
generation, provider session/turn, context cursor, and lease. Sensitive detail keys
are redacted, and long text is bounded. The records are available in Desktop
Diagnostics and `<workspace-data-root>/run/diagnostics.jsonl`.

If startup reports `unsupported_version`, update the provider CLI. Claude capability
detection requires `--input-format`, `--output-format`, `stream-json`, and `--resume`;
Codex capability detection occurs through App Server initialization.
