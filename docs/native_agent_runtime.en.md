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

These five are the only AI CLI tools GuildBotics can run. Supporting a new one
means implementing a native adapter in this repository; there is no way to add an
unsupported tool by dropping a YAML file into a workspace.

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

Each tool still reads its own definition under
`intelligences/cli_agents/<tool>/`: that file carries the `parameters:` and
`effort:` overlay described in the
[custom command guide](custom_command_guide.en.md), which is how the
provider-neutral `low` / `high` levels become provider settings, plus the
`network:` block described below. The shipped defaults also declare
`effort_fields:`, the descriptors the settings editor uses for typed editing.

## Sandbox contract

Every AI CLI turn is confined by the same contract, whatever it is for (a
ticket, a chat, the Desktop troubleshooter, command authoring). GuildBotics asks
for three things and each adapter translates them into its provider's own
sandbox settings. A request the provider cannot enforce on the current OS stops
the agent from starting; it is never widened into something the provider can do.
Codex's native Windows sandbox reads a fixed set of machine-wide roots and has
not been verified to honour the profile's read paths or the network proxy, so
Codex does not start on Windows until that check is done (it does under WSL,
as Linux).

- **Working directory**: the turn's `cwd` (the member's clone for ticket work,
  `<workspace>/.guildbotics/local/work/...` for internal turns) is always
  readable and writable. The workspace's `.guildbotics/config` and `state` are
  not part of it.
- **Beyond the working directory** there are three things. **Trees derived
  from the PATH**: what you can run, agents can run. For each directory on
  the PATH this device gives agents, the smallest tree holding it and the
  packages its executables link into (`/opt/homebrew` for Homebrew's
  `/opt/homebrew/bin`, `~/.local` for `~/.local/bin`) opens for reading.
  There is no setting; every device derives this for every turn. The whole
  tree opens, so every other command in it becomes runnable too: the sandbox
  guards credentials, network and write locations, not the list of programs
  that may start. A tree inside a credential directory (`~/.ssh`, a provider's
  own directory) stays closed and is shown with the reason. **documents**:
  directories under the home directory the work reads from or writes to
  (`read` / `read_write`, relative paths only), created before a turn starts
  when missing, shared in `intelligences/cli_agent_filesystem_grants.yml`.
  **This device's own settings**: extra paths the derivation cannot cover (a
  cache a tool writes, data it keeps outside its install tree; absolute paths
  allowed, must exist) and `deny` entries that close a tree or a corner of
  one, in `local/cli_agent_filesystem_grants.yml`, never synchronized. The
  credential directories are always closed by a built-in deny, so a tree that
  contains one (`~/.local` around `~/.local/share/keyrings`) can open around it.

  ```yaml
  # config/intelligences/cli_agent_filesystem_grants.yml (shared)
  documents:
    - path: Documents/shared-documents
      access: read
    - path: Projects/generated-assets
      access: read_write
  ```

  ```yaml
  # local/cli_agent_filesystem_grants.yml (this device only)
  paths:
    - path: .cache/uv
      access: read_write
  deny:
    - /opt/homebrew/etc
  ```

- **Network**: the `network:` block of the selected tool definition
  (`cli_agents/<tool>/<slot>.yml`) states, separately, what shell commands and
  their child processes (`command`) and the tool's built-in web search / URL
  fetch (`web`) may reach. `mode` is one of `deny` / `allowlist` /
  `unrestricted` (`off` would read as a YAML boolean), `allowed_domains` is
  used only with `allowlist`, and `allow_local_network` opens localhost and the
  LAN to commands. The shipped default closes both routes. A slot that omits
  `network:` inherits the whole block from its tool's `default.yml`; a slot
  that states it states all of it (a partial block is an error).

  ```yaml
  network:
    command:
      mode: allowlist
      allowed_domains: [registry.npmjs.org]
      allow_local_network: false
    web:
      mode: deny
      allowed_domains: []
  ```

All of this is edited in Desktop under **LLM / AI CLI tools → Advanced settings**.
The Network fields of each tool definition disable the modes the tool cannot
enforce on this OS and say why. The "Directories shared by the workspace" card
holds the documents; the "Directories on this device" card lists what this
device's PATH makes readable (each row can be denied), the paths and denies
added here, and what stays closed with its reason; both judge a typed or picked
path before it is added (whether it exists here, whether it holds credentials).
Every member's slots are resolved the way a turn is started: a member whose
slot cannot start on this device is marked in the member list with the reasons,
and the status band at the top says so until the setting is changed. The band's
link opens the setting it is about: the member's slot for a network problem,
the device's directory card for a grant problem.

The provider's own model traffic and login, and the localhost member broker,
are outside this setting. Which modes a provider can enforce is declared in the
catalog in `guildbotics/intelligences/cli_agents.py`; saving a definition checks
that some supported OS can enforce it, and starting an agent checks the current
OS, both against the same catalog. Only Codex translates the contract so far;
the other adapters keep the fixed behaviour described below.

Codex receives the contract as configuration overrides at launch: a permission
profile named `guildbotics`, selected through `default_permissions`. The profile
is built from Codex's platform paths (`:minimal`) rather than its `:workspace`
baseline, whose system-wide read would expose `~/.ssh` and `~/.codex/auth.json`:
writes inside the workspace roots and temp, reads on the directories Codex's own
binary is run through (the invoked link and its real location, never `~/.codex`
itself), reads on the skill roots Codex scans (`~/.agents/skills`,
`~/.codex/skills`, `~/.codex/_skills`, whichever exist, because the agent reads a
skill's body from inside the sandbox), and each filesystem grant as `read` or
`write`.
`command` `deny` becomes `network.enabled=false`; `allowlist` becomes
`network.enabled=true` with `features.network_proxy=true` and the domain rules;
`unrestricted` becomes `network.enabled=true`. `web` `deny` becomes
`web_search="disabled"` and `allowlist` becomes
`tools.web_search.allowed_domains`. Neither `thread/start` nor `turn/start`
carries a sandbox of its own, because either would override the profile. Codex
always uses the non-interactive `never` approval policy, and any unexpected
approval request is declined.

Grok Build always launches with `--sandbox workspace`. The launch command is fixed as
`grok --no-auto-update --sandbox <profile> agent --always-approve stdio`; no arbitrary
CLI flag can be injected from configuration. `--always-approve` is always paired with a
sandbox, and an unexpected ACP `session/request_permission` is declined and recorded.
The decline uses the option id the agent supplied for its `reject_once` option (falling
back to `reject_always`); option ids are chosen per request, so the option kind is never
sent back as an id. When no rejecting option is offered, the request is answered with
`cancelled` rather than reinterpreted as an allow.
`--no-auto-update` keeps a headless run from updating the CLI mid-session, and
GuildBotics never rewrites the user's `config.toml`.

GitHub Copilot runs with its own default boundary (file access confined to the
working directory and the system temp directories). The launch command is fixed as
`copilot --acp --no-auto-update --no-remote-export`; no arbitrary CLI flag can be
injected from configuration. `--no-remote-export` keeps a member's
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

The contract is not translated for Antigravity yet. `agy --sandbox` restricts
terminal commands only; its own file-writing tools still reach outside the
working directory.

Antigravity runs every turn with `--dangerously-skip-permissions` and with
`--add-dir <cwd>`. That second flag is not optional: without it `agy` resolves every tool, including `run_command`,
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
arbitrary shell execution inside the workspace. This is tracked for the day
`agy` grows a real read-only mode.

Claude Code always runs non-interactively with `bypassPermissions`, preserving the
previous `--dangerously-skip-permissions` behavior. GuildBotics also passes a
session-level `sandbox.enabled=false` override because the Bash sandbox is not
compatible with the full range of ticket and chat workflow commands. A higher-priority
Claude managed policy remains authoritative. Claude permission and sandbox settings
are not stored in the workspace policy or exposed in Desktop.

The effective policy and every approval decision are written as provider-neutral
diagnostics events. Claude `bypassPermissions` can modify files
outside the workspace. Use them only with the documented credential isolation and in
an environment whose host access is acceptable. Invalid types, removed keys, and
unknown values fail validation instead of silently changing the effective boundary.

## Authentication

Install and authenticate each selected CLI before starting GuildBotics. Use the
provider's normal interactive login (`codex login`, `claude auth login`, `grok login`,
or `copilot login`) as the same OS user that runs the GuildBotics service. Provider
credentials stay in the provider's own credential store. GuildBotics does not copy them
into its conversation store or diagnostics.

For Grok Build, GuildBotics selects only one advertised authentication method: the saved
login `cached_token`. The API key method is never used -- a key could only reach the
process through the environment, and credential-named variables are stripped from the AI
CLI environment as described below. The browser-based `grok.com` flow is never started
during a headless run; without a saved login the turn fails as an authentication error
that points at `grok login` (or `grok login --device-auth`). Only the chosen method id
is recorded; the contents of `~/.grok/auth.json` are never read.

GitHub Copilot advertises one method, `copilot-login`, whose metadata tells the client
to run `copilot login` in a terminal. GuildBotics verifies the saved login by calling
ACP `authenticate` with that method, which a logged-in install answers immediately; it
never drives the sign-in itself. A rejected `authenticate`, a missing method, or a call
that does not answer promptly -- the terminal login waiting for a user who is not there
-- all fail the turn as an authentication error pointing at `copilot login`. Only the
method id is recorded; the contents of the Copilot credential store are never read.

GitHub, Git, and SSH write credentials are deliberately removed from native
agent process environments. Every inherited variable whose name contains `TOKEN`,
`SECRET`, `PASSWORD`, `PRIVATE_KEY`, or `API_KEY` is dropped; the rule is a name
pattern rather than a list because a list only ever keeps the secrets nobody
remembered to add to it. Keys stored in the workspace SecretStore are dropped
regardless of their name: `guildbotics secrets set` accepts any key name
(say, `DATABASE_URL`), so the fact that a key was stored is itself the
classification, and the name pattern remains as a safety net for credentials
the operator's shell exports outside GuildBotics. That covers the member's own
`{PERSON_ID}_GITHUB_ACCESS_TOKEN` / `_SLACK_BOT_TOKEN` / `_SLACK_APP_TOKEN` and the LLM
provider API keys (`OPENAI_API_KEY` and friends): all of them are consumed inside the
GuildBotics process, and the member CLI loads its own from the OS keychain, so removing
them changes nothing a member can legitimately do. The helpers and sockets that hand out
a credential on demand (`GIT_ASKPASS`, `SSH_ASKPASS`, `SSH_AUTH_SOCK`) are removed too,
together with the parent process's workspace root, run identity, and execution
delegation. A live delegation is a usable grant rather than a label, so
inheriting one would let a provider process call the member CLI directly and bypass
the boundary its own transport enforces. Only the broker re-injects that metadata from
the execution context and the held lease.

Codex, Claude Code, Grok Build, GitHub Copilot, and Antigravity each receive a
per-adapter HTTP MCP endpoint bound to `127.0.0.1` and an unguessable bearer grant.
Grok Build and GitHub Copilot attach the endpoint through ACP `mcpServers`; Codex and
Claude Code receive process-local MCP configuration. Codex reads the raw token from a
dedicated environment variable through `bearer_token_env_var`, so Codex itself adds the
required `Authorization: Bearer` prefix. Antigravity reads `.agents/mcp_config.json`
from a private auxiliary workspace added with `--add-dir`; the process cwd and primary
workspace remain the member's actual working directory. The single
`guildbotics_member` tool accepts
only tokenized arguments for the fixed `guildbotics member` entrypoint; it cannot choose
an executable, invoke a shell, override the workspace, or act as another person. The
endpoint runs in the GuildBotics process outside the provider sandbox, is usable only
while a turn is active, requires a second grant rotated on every turn, and is stopped
with the adapter. Provider processes never receive the member execution lease or
delegation identity.

The broker launches the member CLI as a separate trusted process, where OS Keychain and
other SecretStore backends remain available. It supplies the active turn's short-lived
lease only to that process. The CLI's `--workspace` always names the selected
GuildBotics workspace root, while the child process cwd remains the member's isolated
working directory; the workspace data root may be overridden independently. A read-only
turn supplies no delegation, so the existing member CLI guard rejects every
write-capable command. Every native adapter uses this same member capability boundary.

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

If a bounded snapshot cannot be built safely from the live Slack API, only a new or
rotated session uses the `inspect_required` fallback. A healthy native resume
continues from its provider session and the latest event, so that fallback never
causes a full-history duplicate. The cursor is persisted
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
uses `rate_limit_event` (whose epoch `resetsAt` becomes the exact `retry_after_at`
and takes precedence over the retry delay of `system/api_retry`); Codex uses
account/rate-limit RPC data. GuildBotics does not parse human stderr text for these
decisions.

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
detection requires `--input-format`, `--output-format`, `stream-json`, `--resume`,
`--mcp-config`, and `--strict-mcp-config`. Claude Code before 2.1.246 could still wait
for approval of project MCP servers despite strict mode, so a workspace containing
`.mcp.json` also requires 2.1.246 or later;
Codex capability detection occurs through App Server initialization. ACP capability
detection requires protocol version 1 plus either `loadSession` or
`sessionCapabilities.resume`; every ACP adapter additionally requires HTTP MCP support
for its trusted member capability transport. It never gates on the version string, so
any newer CLI that still exposes those capabilities keeps working. Antigravity
capability detection reads `agy --help` (which prints to stderr and exits 0) and
requires `--print`,
`--output-format`, `--conversation`, `--model`, `--effort`, and `--add-dir`. The
verified baselines are Grok Build 0.2.118 and GitHub Copilot CLI 1.0.77. Antigravity
1.1.11 exposes the required flags; loading MCP configuration from an added auxiliary
workspace remains an explicit machine-verification item before the adapter is declared
supported for the trusted member transport.

Grok rate limits are classified as `rate_limited` only when ACP or an xAI extension
returns structured data; stderr text and assistant prose are never parsed. The xAI
retry-state notice counts as that structured data for the whole turn: when Grok Build
reports `is_rate_limited` and then ends the turn with a code-only RPC error, the
failure is classified `rate_limited`, which routes the workflow into its rate-limit
deferral instead of retrying the agent. No equivalent of Codex
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
