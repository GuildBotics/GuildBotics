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
provider-neutral `low` / `high` levels become provider settings. The shipped defaults also declare
`effort_fields:`, the descriptors the settings editor uses for typed editing.

## Isolated agent environment: access permissions

Every AI CLI turn runs inside an isolated agent environment: a microVM
GuildBotics boots for the one turn from a snapshot it built on this device,
and discards when the turn ends. What the turn may reach is the access
contract below, and the environment is what enforces it -- the same way on
every OS and for every provider, because the provider CLI runs inside and
sees nothing else. There is no per-provider translation and no list of what
a provider can or cannot enforce: a setting the environment cannot honour on
this device (no runtime, no snapshot, no login) stops the agent from
starting, and is never widened.

The environment's runtime is [microsandbox](https://microsandbox.dev/) (a
libkrun microVM). GuildBotics ships it and, the first time it is needed,
places it under `~/.guildbotics/data/msb` and points the SDK at that path
(`MSB_HOME` / `MSB_PATH`): nothing is downloaded, and nothing outside
GuildBotics decides which runtime runs. The hardware virtualization it
needs is Apple Silicon on macOS, the Windows Hypervisor Platform on Windows
11, and KVM on Linux. On Windows the runtime listens on a socket that
Windows Defender Firewall asks about, so a rule for the fixed path is
created once with an elevation prompt. Inside the environment a Windows host
path appears with its drive letter as the top-level directory (`C:\work`
is `/c/work`); the working directory is bound under the same convention.

What the environment holds -- the base image and the provider CLIs GuildBotics
installs at pinned versions -- is built per device as a snapshot and rebuilt
when it no longer matches the declaration. Additional development tools belong
in the base image, not in a package list. The
**Agent execution environment** screen in the Desktop shows the runtime, the
base image, the snapshot's state (with a
build button), the assigned resources, the network policy, the DNS resolvers,
and each tool's login; `guildbotics
environment status` / `build` / `login` are the same state and actions from
a terminal. While the service runs, a changed base image (including one that
arrived from another device through synchronization) is rebuilt by itself;
resource, network, and DNS changes apply when the next microVM boots. While
the image builds, or whenever the environment is unusable, the
ticket patrol and chat dispatch are deferred rather than failed. Why a turn
cannot start here (no runtime, an unreadable declaration, a base image not
loaded, an unbuilt snapshot, no login) is shown in the same words in the
alert band at the top of the screen.

The base image is GuildBotics' own by default (Debian + Node.js + npm + git
+ uv). A workspace that needs another toolchain -- a Python interpreter,
Rust, a browser -- declares an image it built itself. The image's content is
not shared: load its `docker save` archive on each device with `guildbotics
environment image load`, then pick it from the images loaded on this device under **LLM / AI
CLI tools → advanced settings → Environment declaration**, or declare it
with `guildbotics environment image declare`. Images are per CPU
architecture (the environment runs the device's own CPU), so the
declaration carries the reference and, per architecture, the image config
digest (the identity that stays the same across save and load; the same as
the IMAGE ID `docker images` shows). The device that built the images can
declare every architecture at once (`image declare --digest
amd64=sha256:...`), so the other devices only load their archive (`image
load` refuses an archive not built for this device's architecture). A device
runs on the image it loaded under the reference: nothing loaded refuses
turns, while another digest than declared (or nothing declared for its
architecture) runs with a standing warning on the status card, in the alert
band, and in `environment status` that says how to fall in line. The
snapshot is named by the digest the device loaded, so loading the image
again makes it stale, and it is rebuilt.
Build the image `FROM` the default one, or from any image that gives the
build steps what they use (Node.js with `npm`, `curl` and
`tar`), and ship no bubblewrap (`bwrap`): Codex prefers it to the one it
bundles and cannot start a session with Debian's (packages such as
`libwebkit2gtk` pull it in as a dependency; remove the binary then). A loaded image is used without asking a registry (pull policy
`never`). The image for developing GuildBotics itself
(`docker/agent-environment/Dockerfile`; `scripts/build-agent-environment-image.sh`
builds every architecture, loads, and declares) is the worked example.

On macOS, grant Documents folder access once to the app that launches GuildBotics under **System Settings → Privacy & Security → Files & Folders**. During development (`tauri dev`), this is the terminal or Visual Studio Code that started it. GuildBotics checks directory access when displaying environment status and before a turn, and reports the same refusal in the CLI and Desktop if access is denied.

- **Working directory**: the turn's `cwd` (the member's clone for ticket work,
  `<workspace>/.guildbotics/local/work/...` for internal turns) is bound
  read/write at the same path it has on the host. The workspace's
  `.guildbotics/config` and `state` are not part of it.
- **Beyond the working directory** there are two things, both bound at their
  host paths under a home directory that is the host's own. **documents**:
  directories under the home directory the work reads from or writes to
  (`read` / `read_write`, relative paths only), created before a turn starts
  when missing, shared in `intelligences/cli_agent_filesystem_grants.yml`.
  Apart from that file, `Documents/GuildBotics` (the exchange directory) is
  always granted read/write: what the Desktop hands over (a pasted image, a copy of a
  file the environment could not reach) is placed in its `tmp/` and removed
  when the app session ends, a Desktop run that names no working directory
  runs there, and what an agent makes for the user goes under it unless the
  request names a destination. A path the Desktop puts in the input field is
  spelled as the environment names it (`/c/...` on Windows, never `C:\...`), so
  the agent opens it as written.
  **This device's own settings**: extra paths (absolute paths allowed, must
  exist) and `deny` entries that close a corner of what is open, in
  `local/cli_agent_filesystem_grants.yml`, never synchronized. Credential
  directories (`~/.ssh`, a provider's own directory) and the workspace's own
  `.guildbotics` are always closed by a built-in deny. Nothing else of the host exists inside: not its PATH, not
  its other clones, not its keychain. The agent's tools are the environment's
  own, declared in `config/intelligences/agent_environment.yml` (see
  [`guildbotics environment`](cli_reference.md#guildbotics-environment)).

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
    - Documents/shared-documents/private
  ```

- **Resources**: `resources:` in `intelligences/agent_environment.yml` assigns
  memory in MiB and virtual CPUs to every turn and snapshot build. Omitting it
  uses 4096 MiB and 2 vCPUs. This declaration is workspace-wide and the same
  values are used on every device, so choose values that fit the device with
  the least memory and fewest CPU cores.

  ```yaml
  resources:
    memory_mib: 4096
    cpus: 2
  ```

  The values apply when the next microVM boots. Changing them does not rebuild
  the disk snapshot. `guildbotics environment status` and the Desktop's
  **Agent execution environment** screen show the assigned values.

- **Network**: one workspace-wide `network:` block in
  `intelligences/agent_environment.yml` states what every member and slot may
  reach, whether through a shell command and its child processes or through
  the tool's built-in web search / URL fetch. `mode` is `deny`, `allowlist`, or
  `unrestricted` (`off` would read as a YAML boolean). `allowed_domains` is
  used only with `allowlist`; `allow_local_network` also opens localhost and
  the LAN. Omitting `network:` means `deny`. The shipped declaration uses an
  allowlist of common GitHub and package-registry hosts as a starting point
  for coding work. `unrestricted` is only an explicit escape hatch: a turn can
  then send workspace content it can read to any Internet host or fetch any
  external payload. Provider API domains and the localhost member broker are
  always reachable regardless of mode. The gateway still blocks every other
  destination. microsandbox does not yet expose a public denied-egress event,
  so GuildBotics records a deliberately narrow, indirect clue instead: explicit
  URLs and host-and-port pairs in structured command, tool, or runtime failure
  events become
  `agent_environment.network_egress_candidate` records in that turn's local
  Diagnostics after known allowed domains and permitted local IP addresses are
  excluded. Successful command output, bare hostnames, and bare IP addresses are
  not scanned, so filenames and timestamps do not become network candidates.
  The extraction reads at most 8 KiB of each existing failure record and keeps
  at most 32 candidates. It does not enable runtime DEBUG logging, start another
  process, change the agent's prompt, or alter its final response. This is
  information the provider emitted during a failure, not proof that microsandbox
  denied the connection. A client that emits only a bare destination, or none at
  all, still leaves no clue until microsandbox provides the event.

  ```yaml
  network:
    mode: allowlist
    allowed_domains: [github.com, api.github.com, registry.npmjs.org]
    allow_local_network: false
  ```

All of this is edited in Desktop under **Agent execution environment**.
The "Directories shared by the workspace" card holds the documents; the
"Directories on this device" card holds the paths and denies added here; both
judge a typed or picked path before it is added (whether it exists here,
whether it holds credentials). Every member's slots are resolved the way a
turn is started: a member whose slot cannot start on this device is marked in
the member list with the reason, and the status band at the top says so until
the setting is changed.

Inside the environment each provider's own sandbox stays on, but never
narrower than the environment: everything the microVM holds is already
allowed, so what the inner sandbox adds is hiding the provider's own
credentials from the agent's commands and keeping provider settings from
changing between turns. Codex runs under a permission profile that mirrors
the environment's mounts -- the whole guest readable, every directory the
environment bound writable or read-only exactly as it was mounted (so a
read/write grant is read/write for Codex's commands too), the working
directory (its `.git` included) and the temporary directories writable, the
network on -- and hides `~/.codex`; the profile never names `/` as writable,
because Codex 0.153 then loses `/dev/null`.
Codex always uses the non-interactive `never` approval policy, and any
unexpected approval request is declined. Claude Code runs with
`bypassPermissions` and `sandbox.enabled=false` (and `IS_SANDBOX=1`, since the
turn is root inside the microVM and Claude Code otherwise refuses that mode as
root). Grok Build launches with
`--sandbox off` and `--always-approve` (its Linux profiles need Landlock, which the
environment's kernel lacks, and Grok refuses to start with a profile it cannot enforce), GitHub Copilot with
`--no-remote-export` and `allow_all: on` (`off` on a read-only turn, where every
request is declined), Antigravity with `--dangerously-skip-permissions`; none of
them takes a flag from configuration. Which providers' inner sandboxes run on
the microVM's kernel is confirmed per provider as each is provisioned; Codex
is, through the bubblewrap it bundles (a bubblewrap installed in the image is
preferred to it and cannot exec Codex's helper, so the image ships none).

Only the provider's sessions and its account files that hold no credential
survive a turn, bound from this device's store
(`~/.guildbotics/data/agent_environment/<provider>/`) and shared by every
member. The login is never in a turn (see "Logins kept outside the microVM"). The provider's settings and skills are the snapshot's and return to it
every turn. Read-only turns are enforced by the member broker, which holds no
person lease for them and refuses every write-capable member command; what a
provider does with its own file tools on such a turn is recorded on the
turn's approval event and is not a boundary.

The effective policy and every approval decision are written as provider-neutral
diagnostics events. Invalid types, removed keys, and unknown values fail
validation instead of silently changing the effective boundary.

## Authentication

The AI CLI tools run inside the isolated environment, so nothing is installed on the
host and a host login is not what a turn uses. Logging in happens inside the
environment: on every device that runs turns, run `guildbotics environment login
<tool>` (`codex` / `claude` / `grok` / `copilot` / `antigravity`) in a terminal. The tool's own login command starts inside
the environment and walks you through its device code flow in the browser. The result
is sealed on the device (see below) and shared by every member and workspace on that
device. GuildBotics does not copy them into its conversation store or
diagnostics. The Desktop shows the login state and the command to run; it never
drives the login dialogue itself.

The login runs on a terminal inside the environment, because a tool that must ask
before it stores its credentials in a file only asks on one: Copilot, finding no system
keychain there, confirms plain-text storage under its state root once. Grok Build uses
its device-code login. Antigravity has no login command; a print-mode run without a
saved login prints the Google sign-in URL and takes the authorization code on standard
input (within 60 seconds). Grok Build's own sandbox profiles need Landlock, which the
environment's kernel lacks, so it runs with `--sandbox off` there; the environment is
the boundary.

The settings card always offers the login command and copy action, including when
credentials are already saved. On macOS / Linux, Desktop shows its managed CLI's
absolute path; Windows uses `guildbotics` from PATH. While the card is open, status
updates every 10 seconds. **Refresh status** checks immediately. **Credentials saved**
only reports that the sealed login opens, not validity. Structured authentication failures from
turns are kept per device and tool, outside the provider's mounted store, and feed
both the card and alerts through `status.py`. A completed login that leaves
credentials, or a later successful turn by any member, clears the failure.
Other errors leave the last known result unchanged. Past failures are guidance,
not startup refusals, so another turn can retry. A file bound from the store (an
account file) follows a file rewritten in place, but renaming another file over it
fails. A name in the store is used only when what it resolves to on the device lies
inside the store: a link a login or a turn left names a place on the device rather than
in the guest, and following it would bind a directory of the device into a turn.

### Logins kept outside the microVM

The AI CLI tools' logins are kept neither in a turn's microVM nor in a plain file on the
device ([#459](https://github.com/GuildBotics/GuildBotics/issues/459)). What follows
takes Claude Code as the example; the tools differ as this table shows.

| Tool | What is sealed | How a turn gets its stand-in | Gateway upstream | Refresh and usage |
|---|---|---|---|---|
| Codex | `~/.codex/auth.json` (a ChatGPT account login) | a stand-in `auth.json`: an unsigned JWT that claims only an expiry and the account (email, plan, account ID) stands for both the access token and the ID token, and the refresh token is empty (`chatgpt_base_url` and the `base_url` of a model provider of GuildBotics' own, `guildbotics`, point at the gateway) | `https://chatgpt.com` (inference at `/backend-api/codex/responses`, the model list, `/backend-api/wham/usage`) | refresh by running `codex debug models` with the access token claiming an expiry that has passed; usage through App Server `account/rateLimits/read` |
| Claude Code | `~/.claude/.credentials.json` | a stand-in credentials file (`ANTHROPIC_BASE_URL` points at the gateway) | `https://api.anthropic.com` | `claude -p /usage` |
| Antigravity | `~/.gemini/antigravity-cli/antigravity-oauth-token` | a stand-in credentials file (the access token, `token_type`, the expiry, and `auth_method` only; no refresh token and no ID token). `CLOUD_CODE_URL` points at the gateway over HTTPS, and `www.googleapis.com` is relayed to the gateway inside the turn | `https://daily-cloudcode-pa.googleapis.com` (seven `/v1internal:` methods) and `https://www.googleapis.com/oauth2/v2/userinfo` | both through `agy -p /usage`, which refreshes a login it is told has expired |
| GitHub Copilot | `~/.copilot/config.json` (JSON after lines of comments; the token under a key named for the account, one account only; no expiry and no refresh token) | a stand-in beginning `gho_` (the only shape Copilot takes) in `COPILOT_GITHUB_TOKEN`; `COPILOT_DEBUG_GITHUB_API_URL` and `COPILOT_API_URL` point at the gateway | `https://api.github.com` (`/copilot_internal/user`, `/copilot_internal/managed_settings`) and `https://api.individual.githubcopilot.com` (`/models` and inference at `/chat/completions`, `/responses`, and `/v1/messages`, as each model supports) | no refresh (a refused login asks for a new one); usage through Copilot SDK server `account.getQuota` |
| Grok Build | `~/.grok/auth/auth.json` (one entry named for the account) | an external auth provider command (`GROK_AUTH_PROVIDER_COMMAND`) prints the stand-in (`GROK_CLI_CHAT_PROXY_BASE_URL` points at the gateway) | `https://cli-chat-proxy.grok.com` | refresh through `grok models`; usage through ACP `_x.ai/billing`, which an external login cannot read, so it is read where the login is |

In a turn's microVM, each tool is pointed at the gateway with the setting that moves its
API. What that relies on is checked by opt-in tests that run real microVMs with synthetic
secrets; run them when a pinned version changes, and when the gateway or the way an
environment is put together changes (on a device with a snapshot, with
`GUILDBOTICS_CONTRACT_PROBE=1`, `GUILDBOTICS_CONFIG_DIR` at a workspace with the snapshot,
and `-p no:xdist`). Nothing is sent off the device.

- `tests/guildbotics/intelligences/agent_environment/test_provider_contracts.py`: the
  connection contracts. With the production stand-in and settings, which requests reach the
  gateway, that the stand-in is carried nowhere else, and how a refresh is made.
- `tests/guildbotics/intelligences/agent_environment/test_credential_boundary.py`: the
  protection boundary. No real value in any file or process of a turn's microVM; none in an
  answer, even from an upstream that echoes the real token; none written to the writable
  layer or logs on the disk while an environment that holds the login runs (what a power cut
  leaves); nothing of an environment left after a cancel, a timeout, or the GuildBotics
  process being killed; and none in the snapshot. That each check works is shown every time
  by its finding a mark planted where it looks.

- **Where it is kept**: `guildbotics environment login claude` runs with the state root
  (`~/.claude`) in the microVM's memory (tmpfs). The `.credentials.json` the login leaves
  is taken out through the runtime's file transfer while the environment still runs,
  sealed with AES-GCM under a key in the device's keychain, and written to
  `~/.guildbotics/data/agent_environment/claude/login.sealed`. The record binds the tool,
  the account, and the format as authenticated data, so it never opens as anything else.
  The account file (`.claude.json`) holds no secret and stays in the store, bound into
  turns. None of it is a workspace secret, and none of it travels through Git or a Hub.
- **What a turn holds**: the turn's microVM gets a credentials file whose access token is
  a stand-in minted for the turn and whose expiry is far away. The file is built from the
  non-secret fields the catalog names (`scopes`, `subscriptionType`, `rateLimitTier`) alone,
  so neither the refresh token nor any credential a later version adds to the file is in it. Claude Code is pointed at a gateway in the GuildBotics process
  (`auth_gateway.py`) through `ANTHROPIC_BASE_URL`, and the turn's network policy opens
  only the gateway's host port. Anthropic's domains are not open to the turn (the stand-in
  authenticates nothing there anyway). `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` is set.
- **The gateway** forwards only `POST /v1/messages` and `POST /v1/messages/count_tokens`
  carrying that turn's stand-in, only to `https://api.anthropic.com`, and replaces only the
  `Authorization` header with the real token. The guest's `Host`, `x-api-key`, and `cookie`
  are not sent. Redirects are handed back, never followed. Every other destination, route,
  or value is refused before it reaches the upstream. When the turn's microVM stops, the
  gateway stops, and the stand-in opens nothing. When the upstream answers 401, the login
  is refreshed once and the request sent again. Should the real token appear in an answer's
  headers or body, it is masked to the same length before the answer is passed on, and the
  upstream is asked for an uncompressed answer so that the body can be checked; an answer
  compressed all the same is not passed on (502), and the refusal is logged as
  `Gateway refused an answer encoded as ...`. HTTP/1.1 and
  streaming (SSE) are carried; HTTP/2 is declined through ALPN and a WebSocket refused at its
  handshake. The routes not forwarded are logged as `METHOD /path` only.
- **Refresh and usage**: refreshing the token and `/usage` talk to Anthropic's account
  endpoints directly, so the gateway does not carry them. Claude Code runs them itself in
  an environment of their own that holds the login in memory, mounts no working directory
  or workspace, and reaches the provider's domains only. A refresh gives it the login marked
  as expired and runs `claude -p /usage`; the refreshed login is taken out and sealed before
  the environment is stopped. A login within five minutes of its expiry is refreshed before
  the turn's microVM and the tool start: a tool reaches its API as soon as it starts, and
  Antigravity gives up on its sign-in after ten seconds, so the first request never waits
  for a refresh. During a turn, the gateway asks for a refresh five minutes before expiry
  or when the upstream refuses the token, and that request waits for it (every tool that refreshes
  has been seen, with real accounts, to carry on with its turn after that wait). That environment runs one at a time on the device,
  across processes (`login.sealed.lock`), so a refresh token is never spent twice. Turns
  themselves are not serialized, so turns of the same account run side by side. A refreshed
  login that cannot be saved is not continued on the old one: it is recorded as an
  authentication failure and asks for a new login.
- **State**: a locked or unavailable keychain, a missing key, and a record that does not
  open are told apart from a missing login, in the same words `status.py` gives the CLI,
  the Desktop, and a refused turn. Nothing falls back to plain text. When the gateway
  could not use the login during a turn (a refresh that failed, or a login that never
  refreshes being refused), no later request of the turn is given the login either, and
  the turn is an authentication failure that asks for a new login, whatever the tool
  reported; what the tool itself said follows the reason. A tool
  holds only the stand-in, so it may report such a failure as some other error, or return
  the error text as its answer.
- **Codex specifics**: Codex's built-in provider sends inference over a WebSocket to
  chatgpt.com and cannot be pointed at the gateway, so a turn passes, with `-c`, a model
  provider of GuildBotics' own, `guildbotics`, that uses the Responses API over SSE. A thread
  records the provider it started on and resumes on it, so `thread/resume` names
  `guildbotics`, for threads started before the switch as well. The gateway forwards no
  plugins, no ChatGPT connected apps (`codex_apps`), and no analytics, so turns go without
  them. An API key login is not kept: only a ChatGPT account login is sealed.
- **Antigravity specifics**: the Cloud Code API is taken over HTTPS only, so Antigravity's
  gateway speaks TLS with a certificate from a CA made for the turn. The turn trusts, through
  `SSL_CERT_FILE`, a file of the system's CAs with that CA added
  (`/etc/guildbotics/ca-certificates.crt`); the CA's key never leaves the GuildBotics
  process's memory. The eligibility check at start reads a URL no setting moves (the userinfo
  on `www.googleapis.com`), so `/etc/hosts` inside the turn points that host at `127.0.0.2`,
  where a Node TCP relay connects it to the gateway (which presents a certificate for that name
  too). `HTTPS_PROXY` is not used: it would route every request of the tool through the host.
  Only the profile picture (`lh3.googleusercontent.com`, which carries no credential) is fetched
  straight from the turn. Telemetry that carries the token (`play.googleapis.com/log`) is not
  reachable from a turn, which runs without it. The gateway logs the routes it did not forward,
  as `METHOD /path` only (never a token, a query, or a body).
- **GitHub Copilot specifics**: its login neither expires nor refreshes, so the gateway
  asks for a new login instead of refreshing a refused one. Copilot's hosted read-only
  GitHub MCP server (`/mcp/readonly`, which acts with the user's GitHub permissions) and
  its telemetry are not forwarded. Where the API of an account on another plan (Business,
  Enterprise) should be forwarded is not verified.
- **Switching over**: the earlier plain files (Codex's
  `~/.guildbotics/data/agent_environment/codex/.codex/auth.json`, Claude Code's
  `~/.guildbotics/data/agent_environment/claude/.claude/.credentials.json`, Grok Build's
  `~/.guildbotics/data/agent_environment/grok/.grok/auth/`, Antigravity's
  `~/.guildbotics/data/agent_environment/antigravity/.gemini/antigravity-cli/antigravity-oauth-token`,
  GitHub Copilot's `~/.guildbotics/data/agent_environment/copilot/.copilot/config.json`, and
  the `~/.guildbotics/data/agent_environment/copilot/turns/` a killed turn left)
  are neither read nor bound. Log
  in again with `guildbotics environment login <tool>`, then delete them (macOS / Linux:
  `rm ~/.guildbotics/data/agent_environment/codex/.codex/auth.json`,
  `rm ~/.guildbotics/data/agent_environment/antigravity/.gemini/antigravity-cli/antigravity-oauth-token`,
  `rm ~/.guildbotics/data/agent_environment/copilot/.copilot/config.json`,
  `rm -r ~/.guildbotics/data/agent_environment/copilot/turns`,
  `rm ~/.guildbotics/data/agent_environment/claude/.claude/.credentials.json`, and
  `rm -r ~/.guildbotics/data/agent_environment/grok/.grok/auth`; Windows:
  `del %USERPROFILE%\.guildbotics\data\agent_environment\codex\.codex\auth.json`,
  `del %USERPROFILE%\.guildbotics\data\agent_environment\antigravity\.gemini\antigravity-cli\antigravity-oauth-token`,
  `del %USERPROFILE%\.guildbotics\data\agent_environment\copilot\.copilot\config.json`,
  `rmdir /s %USERPROFILE%\.guildbotics\data\agent_environment\copilot\turns`,
  `del %USERPROFILE%\.guildbotics\data\agent_environment\claude\.claude\.credentials.json`, and
  `rmdir /s %USERPROFILE%\.guildbotics\data\agent_environment\grok\.grok\auth`).
  GuildBotics keeps no code for the earlier format, so it does not delete it for you. Leave
  the sessions (`sessions/`, `projects/`, `session-state/`, `antigravity-cli/conversations/`, and the like) and the cache alone. Copies in past backups cannot be erased.
- **Recovery**: when the state says the keychain is locked, unlock it; when it says the
  login cannot be used, fix the keychain or the disk (free space, for one) and try again.
  A login that cannot be opened (a missing or broken key or record) and a failed refresh
  are replaced by logging in again with `guildbotics environment login <tool>`.
- **What remains**: a compromised turn can still use the allowed API through the gateway
  while it runs, spend the account's quota, and put data into allowed request bodies. This
  does not protect against the host's memory or its administrator.
- **Limits of this scheme**: Codex turns go without plugins, ChatGPT's connected apps
  (`codex_apps`), and analytics, and an API key login is not kept. A Claude Code turn cannot
  read the account's profile (`/api/oauth/profile`). Grok Build's usage cannot be read in a
  turn; it is read where the login is held. GitHub Copilot goes without its hosted GitHub MCP
  server (`/mcp/readonly`) and its telemetry, and where Business and Enterprise accounts go is
  not verified. Antigravity sends no telemetry that carries the token. That GitHub Copilot's login has neither an
  expiry nor a refresh is the provider's own design.

For Grok Build, GuildBotics selects only the advertised method of the external auth
provider command (`_meta.external_provider`), which is how a turn's stand-in reaches it.
The browser sign-in and the API key method are never used: no one is there to answer
the one, and a key never reaches the environment. When the method is not offered, the
turn fails as an authentication error.

GitHub Copilot advertises one method, `copilot-login`, whose metadata tells the client
to run `copilot login` in a terminal. GuildBotics calls ACP `authenticate` with that
method; a turn authenticates with the stand-in in `COPILOT_GITHUB_TOKEN` and is answered
immediately. It never drives the sign-in itself. A rejected `authenticate`, a missing method, or a call
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
retries. Diagnostics use `agent_runtime.*`, `workflow.rate_limited`,
`credential.failed`, and `credential.verified` records correlated by person, run, logical conversation,
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
verified baselines are Grok Build 1.0.34 and GitHub Copilot CLI 1.0.77 and 1.0.86. Antigravity
1.1.11 exposes the required flags; loading MCP configuration from an added auxiliary
workspace remains an explicit machine-verification item before the adapter is declared
supported for the trusted member transport.

Grok rate limits are classified as `rate_limited` only when ACP or an xAI extension
returns structured data; stderr text and assistant prose are never parsed. The xAI
retry-state notice counts as that structured data for the whole turn: when Grok Build
reports `is_rate_limited` and then ends the turn with a code-only RPC error, the
failure is classified `rate_limited`, which routes the workflow into its rate-limit
deferral instead of retrying the agent. For account usage, the `_x.ai/billing`
extension in Grok Build 1.0.34 supplies `config.creditUsagePercent`, which GuildBotics
normalizes as the weekly subscription window. `config.currentPeriod` supplies the
window duration and reset time. A gate from `_x.ai/auth/check_subscription`, or usage
at or above 100%, drives the existing rate-limit state. Account types that do not
provide the percentage, including API-key usage, remain unavailable rather than
synthesizing 0%. Authentication stays inside Grok Build's `cached_token` flow;
GuildBotics neither reads the authentication file nor adds a direct HTTP fallback.

GitHub Copilot CLI 1.0.77 reports no token usage over ACP at all: neither the standard
`usage_update` nor a private extension channel carries one. Usage counters therefore
stay empty for Copilot, and the TTL, turn-count, usage, and `context_limit` rotations
that depend on them do not arm on that version. The standard handling is implemented,
so a version that does emit `usage_update` is picked up without a change (1.0.86, the
version the isolated environment pins, was observed sending `usage_update` during a
turn). Copilot rate
limits are likewise classified only from structured RPC error data -- its weekly quota
identifier `user_weekly_rate_limited` among them -- and never from stderr text or
assistant prose; an error that cannot be classified becomes a protocol failure that
rotates the session.

GitHub Copilot's account quota comes from a different path than that ACP turn: the
Copilot SDK server protocol (JSON-RPC with `Content-Length` framing) that the same
CLI serves through `copilot --headless --stdio`. GuildBotics pins GitHub Copilot CLI
1.0.86 in the isolated environment (the 1.0.83 server had no `account.getQuota`, so
the pin moved). After `connect`, `account.getQuota` answers with `quotaSnapshots`
keyed by quota type (`premium_interactions`, `chat`, `completions`, ...), each
carrying `entitlementRequests`, `usedRequests`, `remainingPercentage`, and
`resetDate`. The keys are runtime strings, so they are not filtered against a list:
every finite budget becomes a row labelled with its key, normalized as
`used_percent = 100 - remainingPercentage` with `resetDate` as the reset time when it
lies ahead of the probe (the API has been measured answering every snapshot with the
request's own instant as `resetDate`; a reset already passed names no coming reset
and is dropped). An
unlimited budget (`isUnlimitedEntitlement`, or a negative `entitlementRequests`; the
measured unlimited `chat` / `completions` report `entitlementRequests: 0` with
`remainingPercentage: 100`) gets no meter, and a snapshot whose
`remainingPercentage` is missing, non-numeric, non-finite, or outside 0-100 is
dropped. The period length is not reported, so no duration is guessed from the
reset date. A budget at 0% remaining sets `limit_reached`. The Copilot CLI reads it
itself, in an environment of its own that holds the login in memory; GuildBotics adds
no direct HTTP call. The probe starts no turn and spends no quota.

Antigravity reports per-turn token counts (`input_tokens`, `output_tokens`,
`thinking_tokens`, `cache_read_tokens`, `total_tokens`), which are normalized onto
the same shared keys every other adapter uses. It reports no absolute session
context size, so context-usage rotation does not arm for Antigravity; only the TTL,
turn-count, and token-total limits do. This is the same situation as Grok.

Account quotas are a separate path from those per-turn counters. From Antigravity
CLI 1.1.11, `agy -p "/usage" --output-format json` answers the read-only `/usage`
slash command without starting an agent turn, spending quota, or leaving a
conversation. GuildBotics pins Antigravity CLI 1.2.1 in the isolated environment
and verified the structured payload on 1.2.5: `command.data.groups[].buckets[]`
carries each model group's `window` (`weekly` / `5h`), `remaining_fraction`, and
`reset_time`. Those buckets become the same usage meters the Activity view already
shows for Claude, Codex, and Grok. A `window` other than `weekly` / `5h` keeps
its raw value in the label so rows stay distinguishable; GuildBotics does not
guess a duration from the name. As with Claude and Codex, `limit_reached` is
true when any window is exhausted, so the member badge can light while another
model group still has quota. Accounts that do not expose quotas, and payloads
that are not that JSON, stay unavailable; GuildBotics does not parse the TUI,
the status line, or tab-separated text, and it does not synthesize 0% or 100%.
