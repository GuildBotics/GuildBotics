# Native Agent Runtime

GuildBotics uses native protocol adapters for Codex, Claude Code, Grok Build,
GitHub Copilot, and Antigravity. Codex is driven through
[Codex App Server](https://developers.openai.com/codex/app-server); Claude Code is
driven with its documented `stream-json` input/output and an exact `--resume` session
id; Grok Build and GitHub Copilot are driven through the
[Agent Client Protocol](https://agentclientprotocol.com/protocol/v1/initialization)
(ACP) v1, which `grok agent stdio` and `copilot --acp` serve over stdin/stdout. The two
ACP adapters share one ACP client, and that client shares a line-based JSON-RPC
transport with Codex; only the dialect and reverse-request handling differ. Each
provider adapter adds just its own launch command, authentication, session
configuration, and private notification channels.

Antigravity is the exception to the "one process, many turns" shape. `agy` has no
resident server mode -- the only programmatic entry point is a single
`agy --print --output-format stream-json` run -- so one turn is one process, and
session identity is carried by `--conversation <id>` rather than by a living
process. Everything else (exact resume, streamed events, token usage, structured
error classification) works the same way as for the other native tools.

## Configuration

Select a native provider directly in `intelligences/cli_agent_mapping.yml`:

```yaml
default: codex
codex: codex
claude: claude
grok: grok
copilot: copilot
antigravity: antigravity
```

A native tool still reads its own definition under
`intelligences/cli_agents/<tool>/`: that file carries the `parameters:` and
`effort:` overlay described in the
[custom command guide](custom_command_guide.en.md), which is how the
provider-neutral `low` / `high` levels become provider settings. What such a
definition must not carry is `script:` and `env:`; those belong to a
script-driven tool, and a native adapter ignores them.

The only user-configurable runtime *boundary* is the per-adapter filesystem
scope in `intelligences/native_agent_policy.yml`:

```yaml
codex:
  filesystem_access: workspace

grok:
  filesystem_access: workspace

copilot:
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

For Grok Build, `workspace` maps to `--sandbox workspace` and `host` maps to
`--sandbox off`. The launch command is fixed as
`grok --no-auto-update --sandbox <profile> --always-approve agent stdio`; no arbitrary
CLI flag can be injected from configuration. `--always-approve` is always paired with a
sandbox, and an unexpected ACP `session/request_permission` is declined and recorded.
The decline uses the option id the agent supplied for its `reject_once` option (falling
back to `reject_always`); option ids are chosen per request, so the option kind is never
sent back as an id. When no rejecting option is offered, the request is answered with
`cancelled` rather than reinterpreted as an allow.
`--no-auto-update` keeps a headless run from updating the CLI mid-session, and
GuildBotics never rewrites the user's `config.toml`.

For GitHub Copilot, `workspace` is Copilot's own default -- file access is confined to
the working directory and the system temporary directory -- and `host` adds
`--allow-all-paths`, which removes that verification. A read-only turn keeps the
confined scope even for a member configured with `host`: it reads untrusted recorded
state such as logs, tickets, and chat, reads inside the allowed paths are never asked
about, and its own reply would carry anything it read back out. Declining its writes
does not close that path, so the scope itself is narrowed. The launch command is fixed
as `copilot --acp --no-auto-update --no-remote-export [--allow-all-paths]`; no
arbitrary CLI flag can be injected from configuration. `--no-remote-export` keeps a member's
session from being exported to, or steered from, GitHub web and mobile: it carries
workspace contents and must take its instructions from GuildBotics alone.

Copilot's approval policy is a session configuration option rather than a launch flag,
so it is set per turn. A normal turn runs with `allow_all: on` and never asks. A
read-only turn runs with `allow_all: off`, which makes Copilot ask before every write,
shell command, and URL fetch -- and every one of those requests is declined and
recorded, while reads inside the allowed paths still run automatically. The decline
follows the same rule as Grok's: the option id Copilot supplied for `reject_once`
(falling back to `reject_always`), and `cancelled` when no rejecting option is offered.

The turn's model and reasoning effort are session configuration options too, applied
with `session/set_config_option` after the session is created or reloaded. Copilot
acknowledges an unknown option id with an empty result instead of an error, so the
adapter reads the option list Copilot returns and reports the values the session
actually ended up with as an `agent_runtime` settings event -- never the requested ones.
An option Copilot did not apply is listed as rejected and logged as a warning. Because
these options can be set on any live session, changing effort or model never rotates
the session.

Antigravity takes its model and effort as command-line flags on every turn, and a
resumed conversation honors a changed `--model`, so a settings change never rotates
the session there either. `--model` and `--effort` are mutually exclusive: every id
`agy models` offers either carries its tier in the id (`gemini-3.6-flash-low`) or
rejects `--effort` outright (`claude-sonnet-4-6`), and `agy` refuses the turn when
both are given. A slot that sets both keeps the model and drops the effort, which is
recorded on the settings event. A model that `agy models` does not list is dropped
with a warning; when the catalog cannot be read, validation is skipped rather than
blocking the turn. Because `agy` reports a model back only when one was named on the
command line, a turn without `--model` records an empty model rather than guessing
which account default it ran on.

Antigravity is not part of `native_agent_policy.yml`. `agy --sandbox` restricts
terminal commands only -- its own file-writing tools still reach outside the
working directory -- so there is no file scope to expose as `filesystem_access`
without promising something the flag does not keep.

Antigravity runs every turn with `--dangerously-skip-permissions`, matching the
behavior of the script it replaces, and with `--add-dir <cwd>`. That second flag
is not optional: without it `agy` resolves every tool, including `run_command`,
against its own scratch directory instead of the member's workspace.

**Antigravity cannot enforce a read-only turn at the provider.** None of the
three mechanisms `agy` 1.1.10 offers works: `--mode plan` still writes while
`--dangerously-skip-permissions` is in effect, `--sandbox` confines shell
commands but not the agent's own file tools, and dropping the permission skip
makes headless mode auto-deny every command and return an empty response, which
leaves a read-only turn unable to inspect anything. A read-only turn therefore
runs with the same arguments as a normal one, and every such turn records
`read_only_enforced: false` on its approval event so the gap stays visible in
diagnostics. The other layers still apply: a read-only turn holds no person
lease, so write-side `guildbotics member` commands fail `validate_delegation`,
and the credential isolation below removes the tokens a direct `git push` or
`gh` call would need. What is not blocked is local file modification and
arbitrary shell execution inside the workspace. This is not a regression -- the
script path enforced nothing either -- and it is tracked for the day `agy` grows
a real read-only mode.

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
provider's normal interactive login (`codex login`, `claude auth login`, `grok login`,
or `copilot login`) as the same OS user that runs the GuildBotics service. Provider
credentials stay in the provider's own credential store. GuildBotics does not copy them
into its conversation store or diagnostics.

For Grok Build, GuildBotics selects only an advertised authentication method: the saved
login `cached_token`, or `xai.api_key` when `XAI_API_KEY` is set and that method is
advertised. The browser-based `grok.com` flow is never started during a headless run;
without a saved login the turn fails as an authentication error that points at
`grok login` (or `grok login --device-auth`). Only the chosen method id is recorded;
the contents of `~/.grok/auth.json` are never read.

GitHub Copilot advertises one method, `copilot-login`, whose metadata tells the client
to run `copilot login` in a terminal. GuildBotics verifies the saved login by calling
ACP `authenticate` with that method, which a logged-in install answers immediately; it
never drives the sign-in itself. A rejected `authenticate`, a missing method, or a call
that does not answer promptly -- the terminal login waiting for a user who is not there
-- all fail the turn as an authentication error pointing at `copilot login`. Only the
method id is recorded; the contents of the Copilot credential store are never read.

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

- Healthy native resume (Codex, Claude, Grok, Copilot, or Antigravity): only the latest event is
  added to the context already held by the provider session. The workflow may refresh a bounded snapshot for safe
  future rotation, but that snapshot is not injected into the healthy session.
- New or rotated native session: the bounded snapshot before the event and the latest
  event are injected exactly once.
- A one-shot script with no conversation of its own: the bounded snapshot and latest
  event are injected on every invocation.
- A one-shot script that resumes an exact conversation only within one dispatch
  declares `conversation_scope: dispatch`. Once its saved conversation ID exists, a
  completion retry receives only a continuation instruction and does not receive the
  same event again.

If a bounded snapshot cannot be built safely from the live Slack API, only a new or
rotated session or a non-resuming one-shot invocation uses the `inspect_required`
fallback. A healthy native resume continues from its provider session and the latest
event, so that fallback never causes a full-history duplicate. The cursor is persisted
only after provider terminal success, preserving an unprocessed event after a failed
turn. A completion retry with the same cursor is delivered as a continuation.

Records are atomically stored under
`<workspace-data-root>/agent-runtime/conversations/<person>/<adapter>/`. They contain
provider session/turn ids, cursor, usage counters, the absolute session context
snapshot, health, generation, and rotation reason. They never contain provider
credentials or raw protocol payloads. ACP has no standard provider turn id, so the ACP
adapters leave it empty rather than persisting a transport-local JSON-RPC request id.

GuildBotics never uses a provider's “latest” or implicit continuation mode. A missing
or unhealthy session fails exact `resume`; `auto` starts a new generation and rebuilds
context. Rotation also occurs after cancellation, malformed or incomplete streams,
process failure, provider context compaction, TTL/turn/usage limits, or a model change.
Codex `contextCompaction` and Claude `compact_boundary` events are normalized to the
same runtime event; the completed turn remains successful, while the next dispatch
starts a new generation and rebuilds the Slack snapshot.

ACP has no standard compaction notification, so Grok compaction is normalized from
xAI's `auto_compact_*` extension notifications. Grok Build 0.2.114 does not emit the
standard ACP `usage_update` at all, so the absolute session context snapshot stays empty
and the 90% `context_limit` rotation does not arm on that version. The handling is
implemented for a version that does emit it, where a drop in `used` also serves as a
name-independent compaction signal.

On 0.2.114 the only channel that reports token usage is the xAI `turn_completed`
extension. Its `inputTokens`, `outputTokens`, `cachedReadTokens`, `reasoningTokens`, and
`totalTokens` are normalized to the shared usage keys, so TTL, turn-count, and usage
limits rotate normally. `costUsdTicks`, `modelCalls`, and `apiDurationMs` are not token
counts and are kept in event details so they are never summed with usage.

xAI extension updates arrive on two channels, `_x.ai/session_notification` and
`_x.ai/session/update`; both are handled identically. Every other `_x.ai/*` method is
peer UI state and is aggregated into one record per turn. Channels whose payloads were
inspected on 0.2.114 (`_x.ai/queue/changed`, `_x.ai/sessions/changed`,
`_x.ai/settings/update`, `_x.ai/announcements/update`, and similar) echo the submitted
prompt text, the workspace path, or promotional copy, so only their counts are kept. A
channel that has not been seen before additionally records its payload's top-level field
names. No payload value is ever stored: the diagnostics redactor operates on mapping
keys, so a serialized payload would carry a secret through verbatim.

Tool calls are classified from the ACP `kind`: `execute` becomes a command and
`edit`, `delete`, or `move` become a file change. `locations` lists every file a tool
touched, reads included, so it records the affected paths without classifying the call.
A `tool_call_update` may carry only `toolCallId`, so the kind declared when the call
started is retained per `toolCallId` and applied to later updates.

Reasoning arrives as `agent_thought_chunk` and is recorded for transcripts, but only
`agent_message_chunk` builds the reply.

ACP answers `session/prompt` only when the turn ends, so that one request carries no
per-request deadline; the turn as a whole is bounded by the turn timeout, which sends
`session/cancel` and stops the process group when it expires. Requests that answer
immediately, such as initialize and session load, keep their per-request deadline.

A session the running process still holds open is never reloaded. The conversation
never left the process, so there is no history to rehydrate, and Copilot answers a
second load with `already loaded`. Only a restarted process, which has nothing but the
session id stored on the conversation, performs the reload. The turn's settings are
still re-applied either way.

Exact ACP resume uses `session/resume` when advertised and `session/load`
otherwise. Neither Grok Build 0.2.114 nor GitHub Copilot CLI 1.0.77 advertises
`sessionCapabilities.resume`, so both take the `session/load` path. `session/load` replays the whole transcript before it answers, so that
response is the boundary: replayed history is excluded from the current turn's events,
from Slack, and from the normal transcript, and only the replayed count is recorded.
History replays on the xAI extension channels as well as the standard one and includes
the previous turn's `turn_completed` token usage, so replayed updates are counted but
never decoded; an earlier turn's usage is never reported as the current turn's.

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

Antigravity is classified from its terminal `result` event: a `status` other than
`SUCCESS` is a failure, and the accompanying `error` field decides the category.
`agy` 1.1.10 reports quota and credential failures as prose rather than as a code,
so that one field is matched against a small anchored pattern set kept in the
adapter; the recovery time it may carry (`Resets in 1h23m`) is passed through the
same normalization every other tool uses. A rate limit does not rotate the session;
authentication, protocol, and process failures do.

When a reset timestamp is available, ticket selection and the chat pending queue defer
the next attempt until that exact time. They do not consume in-process completion
retries. Diagnostics use `agent_runtime.*`, `workflow.rate_limited`, and
`credential.failed` records correlated by person, run, logical conversation,
generation, provider session/turn, context cursor, and lease. Sensitive detail keys
are redacted, and long text is bounded. The records are available in Desktop
Diagnostics and `<workspace-data-root>/run/diagnostics.jsonl`.

If startup reports `unsupported_version`, update the provider CLI. Claude capability
detection requires `--input-format`, `--output-format`, `stream-json`, and `--resume`;
Codex capability detection occurs through App Server initialization. ACP capability
detection requires protocol version 1 plus either `loadSession` or
`sessionCapabilities.resume`. It never gates on the version string, so any newer CLI
that still speaks ACP v1 keeps working. Antigravity capability detection reads
`agy --help` (which prints to stderr and exits 0) and requires `--print`,
`--output-format`, `--conversation`, `--model`, `--effort`, and `--add-dir`. The
verified baselines are Grok Build 0.2.114, GitHub Copilot CLI 1.0.77, and
Antigravity 1.1.10.

Grok rate limits are classified as `rate_limited` only when ACP or an xAI extension
returns structured data; stderr text and assistant prose are never parsed. No equivalent of Codex
`account/rateLimits/read` is exposed over ACP (`x.ai/session/usage` answers `Method
not found` on 0.2.114), so GuildBotics does not present a weekly or 5-hour usage meter
for Grok. Activity History treats "no usage
data" as a normal state and never synthesizes an empty window or a 0% figure.

GitHub Copilot CLI 1.0.77 reports no token usage over ACP at all: neither the standard
`usage_update` nor a private extension channel carries one. Usage counters therefore
stay empty for Copilot, and the TTL, turn-count, usage, and `context_limit` rotations
that depend on them do not arm on that version. The standard handling is implemented,
so a version that does emit `usage_update` is picked up without a change. Copilot rate
limits are likewise classified only from structured RPC error data -- its weekly quota
identifier `user_weekly_rate_limited` among them -- and never from stderr text or
assistant prose; an error that cannot be classified becomes a protocol failure that
rotates the session.

Antigravity reports per-turn token counts (`input_tokens`, `output_tokens`,
`thinking_tokens`, `cache_read_tokens`, `total_tokens`), which are normalized onto
the same shared keys every other adapter uses. It reports no absolute session
context size, so context-usage rotation does not arm for Antigravity; only the TTL,
turn-count, and token-total limits do. This is the same situation as Grok.
