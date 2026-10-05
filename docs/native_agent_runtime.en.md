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

Every command runs inside an isolated agent environment: a microVM
GuildBotics boots from a snapshot it built on this device. A command, its
subcommands, and the AI CLI turns among them share one microVM: it boots when
the command starts, able to run every AI CLI tool the member is configured
with, and shaped by the command -- it works in the command's working
directory -- and is discarded when the command ends, whether it succeeded, failed, or was
cancelled. The adapters and the provider CLIs run inside it, so a provider's
output is read there, not on the host. The turns run one at a time and see what an earlier one left in
it, since a command and its subcommands are one isolation. No turn runs
outside a command: the Desktop's assistants and the diagnostics screen's AI CLI
tool check run as bundled commands too. A running microVM is never reshaped: a turn whose working
directory is outside what the microVM mounted when it booted, or that asks
for another access contract, is refused. A tool that is configured but not
logged in stops nothing until a turn of it comes. What the turn may reach is the access
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

What the environment holds -- the base image, the provider CLIs GuildBotics
installs at pinned versions, and GuildBotics' own Python environment -- is built per device as a snapshot and rebuilt
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

GuildBotics' own Python environment is what GuildBotics' code runs with inside
the environment. `/opt/guildbotics/venv` holds Python 3.12 (the image's own
when it has one, otherwise installed by uv under `/opt/uv/python`) and the
pinned dependencies, and `apt-get` installs WeasyPrint's native libraries
(Pango) for `to_pdf` with fonts for Latin and Japanese text
(`fonts-dejavu-core`, `fonts-noto-cjk`). The dependency list is
`guildbotics/intelligences/agent_environment/requirements.txt`, exported
from `uv.lock` without `microsandbox`, which only the host uses (export it
again after changing `uv.lock`; the drift test `test_requirements.py`
prints the command when it fails). The snapshot is named after the content
of every build step, so a changed list makes it stale and it is rebuilt;
commands cannot start during the rebuild (tens of seconds to minutes). The code
itself is not in the snapshot: every command's microVM binds the running process's own
`guildbotics` package (the checkout when running from source, the process's
own build when packaged) read-only at `/opt/guildbotics/code/guildbotics`.
It is not bound at its host path so that a turn working in the GuildBotics
checkout itself does not find its `guildbotics/` covered read-only. Only
`/opt/guildbotics/venv` finds it there, through a `guildbotics.pth` the build
writes into its `site-packages`; GuildBotics' code runs with `python -B` and
no `PYTHONPATH`, so a project a turn tests with its own Python (the
GuildBotics checkout among them) imports its own code, not the bound one.

With the default base image (`node:22.23.2-bookworm`, arm64), the snapshot
build takes about 35 s on macOS (base image already pulled). A snapshot holds
only what the build adds on top of the base image (the writable layer
`upper.ext4`), which uses about 1.8 GB; the whole environment is that plus the
base image. An image that already has what the build installs (Python 3.12,
say) therefore gives a smaller snapshot even when the whole environment is
larger.

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
build steps what they use (Debian's `apt-get`, Node.js with `npm`, `curl` and
`tar`) and git, which [member git](#member-git-and-the-members-clones) runs
the member's clones with (the build checks it last), and ship no bubblewrap (`bwrap`): Codex prefers it to the one it
bundles and cannot start a session with Debian's (packages such as
`libwebkit2gtk` pull it in as a dependency; remove the binary then). A loaded image is used without asking a registry (pull policy
`never`). The image for developing GuildBotics itself
(`docker/agent-environment/Dockerfile`; `scripts/build-agent-environment-image.sh`
builds every architecture, loads, and declares) is the worked example.

On macOS, grant Documents folder access once to the app that launches GuildBotics under **System Settings → Privacy & Security → Files & Folders**. During development (`tauri dev`), this is the terminal or Visual Studio Code that started it. GuildBotics checks directory access when displaying environment status and before a turn, and reports the same refusal in the CLI and Desktop if access is denied.

On Windows, directories under `%LOCALAPPDATA%\Temp` cannot be bind-mounted into the isolated environment ([microsandbox #1692](https://github.com/superradcompany/microsandbox/issues/1692)). If any directory mounted for a command is there -- the working directory of a writable command, a filesystem grant, the workspace itself, or GuildBotics' data under the home directory -- the command is refused at startup with the path and the reason. Move the directory outside the OS temporary directory and try again.

Every host mount source is opened one path component at a time without following
symbolic links or Windows reparse points, before GuildBotics reads or creates
anything there. Use the real directory path. On macOS, only the fixed OS aliases
`/tmp`, `/var`, and `/etc` are saved as `/private/...`. The checked name is passed
unchanged to microsandbox; it is checked again immediately before startup.

A host source containing a protected directory is refused, including a read-only
source. Protected directories include credentials, `~/.guildbotics`, every
registered workspace's `.guildbotics`, and device-local `deny` entries, even when
absent. User working directories and grants also refuse paths inside protected directories. Share a safe child
directory instead of a parent containing private data; a `deny` does not carve a
hole in a larger mount. Existing filesystem objects are compared by identity,
so another spelling of the same directory does not bypass this check.

Opening, creating, or joining a workspace registers its location on this device.
Credential symlinks protect their original location, intermediate links, and actual destination; they
do not prevent commands in unrelated directories. Loops stop after 40 link hops.
Missing or non-directory components before `..` remain untraversable; their inspected
prefix stays protected without blocking unrelated locations. The trusted installed package
is canonicalized once before inspection, and code and templates share that root.
The **Device and hub → Registered workspaces** list includes inactive workspaces;
remove an entry only when its state no longer needs protection. Removing the entry
does not delete files. The selected workspace cannot be unregistered, and removing
another entry requires confirmation. Invalid startup selections leave Desktop
running with no workspace selected and show the refusal reason. Unavailable old
locations can still be unregistered. Malformed grants allow workspace selection
for editing or synchronization, but commands remain refused until repaired.
Copy sources must remain outside protected state. Leaf links are refused. Ancestor
links such as `~/Dropbox/report.pdf` are accepted only when the parent contains fixed
credential state and cannot be shared with a turn; otherwise select the file in its
real directory. The preview shows the refusal and offers no unsafe grant. Status
polling retries an unavailable input store and clears its dedicated alert after recovery.
Member IDs accept lowercase letters, digits, `_` and `-`, excluding Windows reserved names.
Invalid stored configurations show their filename and can be edited, renamed or deleted
in setup, while runtime loading remains refused until repair. Existing directory names
such as `Alice`, `alice.bak`, names with internal spaces, and Unicode names are addressable
for repair. Names with separators, drive or stream suffixes, control characters, or trailing
dots or spaces must be renamed or removed on disk using the reported filename.
Directories without `person.yml` are not members. Copying checks the 20MiB
limit throughout the read, including files that grow during the copy. Workspace
locations must be outside their own grants and
the exchange directory. Move an existing workspace out of those locations before
opening it. Host-created hard links to protected files are not distinguished by copy
admission; turns cannot create such links because protected files are not mounted.

- **Working directory**: the working directory itself opens nothing for
  writing; only a grant does. A command that may write works in it directly,
  bound read/write at its host path, only when a read/write grant (a document
  directory, a device path, or the exchange directory) contains it. Any other
  working directory -- a repository named with `guildbotics run --cwd`, for
  example -- is bound read-only, its `.git` included, copied onto the
  microVM's own disk at the same path, and the command works on the copy.
  When the command ends well, the host writes back the regular files that
  changed in the copy, reaching each from the working directory without
  following a link; links, anything inside `.git`, and anything outside the
  working directory are never written back. If a file changed on the host
  while the command ran, nothing is written back and the command fails naming
  those files; a command that failed has nothing written back. A written file
  keeps its host mode, and its executable bit is written back only when the
  command changed it, refused like a content change when the host changed it
  meanwhile. Write-backs on a device take turns, so of two commands that
  copied the same files -- through one working directory, or one inside the
  other's -- only the first that ends writes back. A working directory
  replaced while the command ran, and two changed names the host takes for
  one file (differing only in case or Unicode form), refuse the write-back
  too. Every file is written beside its place before any is put
  in place; a failure while putting them in place names the files already
  written back. A directory the command removed stays on the host, and a
  link (one in a `.venv` or `node_modules`, for example) is missing from the
  copy. The copy is made on the microVM's own disk (about 4 GB on the default
  image) when the command starts, and its file list is limited to 64 MiB.
  When the original's `.git` is a directory, the copy's `.git` names it
  read-only, so `git status` and `git diff` work and nothing can be
  committed; when it is a file (a `git worktree` checkout or a submodule),
  the repository it names is not mounted, and git sees no repository in the
  copy. A command that may write is refused in a working
  directory only a read-only grant contains, and a read-only command gets an
  empty directory of the microVM's own there instead. Do not keep a
  repository inside the exchange directory or a read/write grant: a command
  can change its `.git` (hooks, configuration) there, and git on the host runs
  what it finds. A command the host
  starts on its own (a scheduled or routine command, the ticket patrol, a
  chat dispatch) works in the exchange directory (`Documents/GuildBotics`,
  below); a Desktop run works where the screen says (the exchange directory
  when it says nothing), and `guildbotics run` in `--cwd` or the shell's
  working directory. A turn works there or anywhere else the microVM
  mounted. The workspace's `.guildbotics/config` and `state` are not part
  of it.
- **The member's clone**: for a command that may write, the running
  member's clone (`<workspace>/.guildbotics/local/clones/<person_id>`), where
  the turns of ticket and chat work run, is bound read/write at its host
  path too, created first when missing. It is opened for its own sake, so it
  is an explicit host-owned child of protected state. A writable command cannot
  work in the workspace root, which contains `.guildbotics`; use a safe project
  directory or the member's clone. Only the running member's clone
  is mounted, and a read-only command gets none.
- **Inspected workspace state**: only for the turns of a command that
  declares it (`inspects`), parts of the workspace's own state are bound
  read-only at their host paths. `diagnostics` is the recorded runs
  (`.guildbotics/local/run`); `config` is the workspace
  configuration (`.guildbotics/config`) and the packaged templates it falls back
  to (`templates` inside the GuildBotics code mount above). The bundled troubleshooting command (`assistants/troubleshoot`) is the only
  one that declares it today; it reads the records against the commands and
  settings they ran with. The declaration is independent of `read_only`: what a turn may change and what it needs to
  read are separate questions.
- **Beyond the working directory** there are two things, both bound at their
  host paths under a home directory that is the host's own. **documents**:
  directories under the home directory the work reads from or writes to
  (`read` / `read_write`, relative paths only), created before a turn starts
  when missing, shared in `intelligences/cli_agent_filesystem_grants.yml`.
  Apart from that file, `Documents/GuildBotics` (the exchange directory) is
  always granted read/write: a Desktop run that names no working directory
  runs there, and what an agent makes for the user goes under it unless the
  request names a destination. What the Desktop hands over (a pasted image, a
  copy of a file the environment could not reach) is kept in GuildBotics' own
  storage (`~/.guildbotics/data/command_inputs`), bound read-only at the same
  path into the runs the Desktop starts, and removed when the app session
  ends. A path the Desktop puts in the input field is spelled as the
  environment names it (`/c/...` on Windows, never `C:\...`), so the agent
  opens it as written.
  **This device's own settings**: extra paths (absolute paths allowed, must
  exist) and `deny` entries that protect directories from sharing, in
  `local/cli_agent_filesystem_grants.yml`, never synchronized. Credential
  directories (`~/.ssh`, a provider's own directory) and registered workspace
  state are protected by the same source checks. Nothing else of the host exists inside: not its PATH, not
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
    - Documents/private
  ```

- **Resources**: `resources:` in `intelligences/agent_environment.yml` assigns
  memory in MiB and virtual CPUs to every command's microVM and every snapshot build. Omitting it
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
  reach, whether through the command's own code, a turn's shell command and its
  child processes, or the tool's built-in web search / URL fetch. A command
  that reaches the network itself needs its destinations allowed here before
  it runs. `mode` is `deny`, `allowlist`, or
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

- **Read-only turns**: a command that may change nothing declares so
  (`read_only: true`; among the bundled commands, the Desktop's troubleshooting
  and command-authoring assistants and the diagnostics screen's AI CLI tool
  check). The declaration is the command's: its whole run, its own code, its
  subcommands, and every turn among them included, is held to it, and no turn
  can make itself read-only.
  The contract (`AccessContract.read_only`) states it, and the environment
  confines it the same way whatever provider runs it. Every directory bound from the host is
  read-only, the exchange directory and `read_write` grants included, and the
  working directory is an empty directory of the microVM's own: writable,
  holding nothing of the host, and discarded with the microVM. The workspace's `network:`
  does not apply: only the provider's API domains and the member broker are
  reachable. Its sessions are bound from a store of its own
  (`agent_environment/<provider>/read-only/`), so the conversation resumes,
  and neither the provider's shared store nor the cache turns share is bound:
  what a read-only turn left there would be resumed or run by the next turn
  that may write. The account files a provider needs are copied in afresh
  every turn. A web tool the provider runs on its own servers (such as a
  hosted web search) never passes through the environment, so it is not
  stopped on any turn. The contract, `read_only`
  included, is recorded on every turn's `started` event (`requested_policy`).
  Such a turn holds no person lease, so the member broker also refuses every
  write-capable member command.

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
read/write grant is read/write for Codex's commands too), a writable working
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
`--no-remote-export` and `allow_all: on`, Antigravity with
`--dangerously-skip-permissions`; none of them takes a flag from configuration,
and a read-only turn launches them the same way. Codex uses its bundled bubblewrap
(a bubblewrap installed in the image is preferred to it and cannot exec Codex's
helper, so the image ships none). Whether a new pinned release's inner sandbox
runs on the microVM's kernel is checked only when optional real-device
verification is requested; unattended pin updates do not establish that it works.

Only the provider's sessions and its account files that hold no credential
survive a turn, bound from this device's store
(`~/.guildbotics/data/agent_environment/<provider>/`) and shared by every
member. The login is never in a turn (see "Logins kept outside the microVM"). The provider's settings and skills are the snapshot's and return to it
whenever a microVM is discarded.

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
fails. Each store path component is opened without following links; a link left
by a login or turn refuses startup.

### Logins kept outside the microVM

The AI CLI tools' logins are kept neither in a turn's microVM nor in a plain file on the
device ([#459](https://github.com/GuildBotics/GuildBotics/issues/459)). What follows
takes Claude Code as the example; the tools differ as this table shows.

| Tool | What is sealed | How a turn gets its stand-in | Gateway upstream | Refresh and usage |
|---|---|---|---|---|
| Codex | `~/.codex/auth.json` (a ChatGPT account login) | a stand-in `auth.json`: an unsigned JWT that claims only an expiry and the account (email, plan, account ID) stands for both the access token and the ID token, and the refresh token is empty (`chatgpt_base_url` and the `base_url` of a model provider of GuildBotics' own, `guildbotics`, point at the gateway); the same stand-in in `CODEX_CONNECTORS_TOKEN` for the connected apps | `https://chatgpt.com` (inference at `/backend-api/codex/responses`, the model list, the rate limits and settings under `/backend-api/wham/`, plugins at `/backend-api/ps/plugins/*` and `/backend-api/plugins/featured`, and the connected apps' MCP server at `/backend-api/ps/mcp`) | refresh by running `codex debug models` with the access token claiming an expiry that has passed; usage through App Server `account/rateLimits/read` |
| Claude Code | `~/.claude/.credentials.json` | a stand-in credentials file (`ANTHROPIC_BASE_URL` points at the gateway) | `https://api.anthropic.com` | `claude -p /usage` |
| Antigravity | `~/.gemini/antigravity-cli/antigravity-oauth-token` | a stand-in credentials file (the access token, `token_type`, the expiry, and `auth_method` only; no refresh token and no ID token). `CLOUD_CODE_URL` points at the gateway over HTTPS, and `www.googleapis.com` is relayed to the gateway inside the turn | `https://daily-cloudcode-pa.googleapis.com` (eight `/v1internal:` methods) and `https://www.googleapis.com/oauth2/v2/userinfo` | both through `agy -p /usage`, which refreshes a login it is told has expired |
| GitHub Copilot | `~/.copilot/config.json` (JSON after lines of comments; the token under a key named for the account, one account only; no expiry and no refresh token) | a stand-in beginning `gho_` (the only shape Copilot takes) in `COPILOT_GITHUB_TOKEN`; `COPILOT_DEBUG_GITHUB_API_URL` and `COPILOT_API_URL` point at the gateway | `https://api.github.com` (`/copilot_internal/user`, `/copilot_internal/managed_settings`) and `https://api.individual.githubcopilot.com` (`/models` and inference at `/chat/completions`, `/responses`, and `/v1/messages`, as each model supports; also the GitHub MCP server at `/mcp/readonly` and custom agents at `/agents/swe/custom-agents/*`) | no refresh (a refused login asks for a new one); usage through Copilot SDK server `account.getQuota` |
| Grok Build | `~/.grok/auth/auth.json` (one entry named for the account) | an external auth provider command (`GROK_AUTH_PROVIDER_COMMAND`) prints the stand-in (`GROK_CLI_CHAT_PROXY_BASE_URL` points at the gateway) | `https://cli-chat-proxy.grok.com` | refresh through `grok models`; usage through ACP `_x.ai/billing`, which an external login cannot read, so it is read where the login is |

In a turn's microVM, each tool is pointed at the gateway with the setting that moves its
API. What that relies on is checked by opt-in tests that run real microVMs with synthetic
secrets when the gateway or the way an environment is put together changes.
Real-device verification is optional for a pin update and is not part of the
unattended update workflow. When a human requests it during PR review, use
the [verify-ai-cli-tools skill](../.agents/skills/verify-ai-cli-tools/SKILL.md)
to record the results on that PR after execution-specific approval and with
the human watching. Run the tests on a device with a snapshot, with
`GUILDBOTICS_CONTRACT_PROBE=1`, `GUILDBOTICS_CONFIG_DIR` at a workspace with the snapshot,
and `-p no:xdist`. Nothing is sent off the device by these synthetic-secret tests.

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
  or value is refused before it reaches the upstream. The gateway lives as long as the
  microVM, since its port is opened when the microVM boots, but it is lent to one turn at a
  time: once the turn ends its stand-in opens nothing, and the next turn is lent a login and
  a stand-in of its own (a login one turn could not use is not the next turn's failure).
  When the microVM stops, the gateway stops. When the upstream answers 401, the login
  is refreshed once and the request sent again. Should the real token appear in an answer's
  headers or body, it is masked to the same length before the answer is passed on, and the
  upstream is asked for an uncompressed answer so that the body can be checked; an answer
  compressed all the same is not passed on (502), and the refusal is logged as
  `Gateway refused an encoded answer to METHOD /path` alone. The gateway's logs carry
  nothing the upstream sent, and the status line's reason phrase httpx logs at INFO has the real token
  masked, for a process run with a root handler that lets INFO through (GuildBotics sets no root
  logger, and undoes what the MCP SDK the member broker uses sets, so by default the line is
  not logged). httpcore's DEBUG trace records an answer's headers as they came, so
  running with the root logger at DEBUG can leave the real value in a log. HTTP/1.1 and
  streaming (SSE) are carried; HTTP/2 is declined through ALPN and a WebSocket is never
  upgraded to (its handshake is taken as a plain HTTP request). The routes not
  forwarded are logged as `METHOD /path` only, the same route once per turn. A catalog route
  matches its path exactly; one that ends in `/*` (for a tool that puts a repository or
  the like in the path) forwards the paths under it made of plain names only (never one
  with a segment that is empty or begins with `.`, a percent-encoding, or a character outside
  ASCII).
- **TLS**: for tools that require HTTPS, the gateway uses a certificate from a CA made
  for each turn. Through `SSL_CERT_FILE`, the turn trusts the system's CAs with that CA
  added in `/etc/guildbotics/ca-certificates.crt`. The CA's key never leaves the
  GuildBotics process's memory. Codex and Antigravity use this mechanism.
- **Refresh and usage**: refreshing the token and `/usage` talk to Anthropic's account
  endpoints directly, so the gateway does not carry them. Claude Code runs them itself in
  an environment of their own that holds the login in memory, mounts no working directory
  or workspace, and reaches the provider's domains only. What the tool (or its provider
  through it) says there of a usage it could not read reaches a log or a screen with the
  login masked in it: every value of the login but the fields a turn is lent and values too
  short to be a credential. A refresh gives it the login marked
  as expired and runs `claude -p /usage`; the refreshed login is taken out and sealed before
  the environment is stopped. A login within five minutes of its expiry is refreshed before
  the turn and the tool start: a tool reaches its API as soon as it starts, and
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
  `guildbotics`, for threads started before the switch as well. The MCP server of ChatGPT's
  connected apps (`codex_apps`) is authenticated by `CODEX_CONNECTORS_TOKEN` (Codex attaches
  no login to it from a stand-in), so the same stand-in is given in that variable too.
  Analytics are not forwarded. An API key login is not kept: only a ChatGPT account login is sealed.
  Starting with Codex 0.156, account reads and turns first discover workspace routing
  through `GET /backend-api/wham/accounts/check`. The gateway forwards that route over
  HTTPS. With the observed
  `workspace_backend_origin: NO_CONSTRAINT`, Codex retains the configured gateway origin
  but still requires HTTPS. The discovery response is forwarded unchanged; routing policies
  that require a different backend origin have not been verified.
- **Antigravity specifics**: the Cloud Code API requires HTTPS, so Antigravity uses TLS.
  The eligibility check at start reads a URL no setting moves (the userinfo
  on `www.googleapis.com`), so `/etc/hosts` inside the turn points that host at `127.0.0.2`,
  where a Node TCP relay connects it to the gateway (which presents a certificate for that name
  too). `HTTPS_PROXY` is not used: it would route every request of the tool through the host.
  Only the profile picture (`lh3.googleusercontent.com`, which carries no credential) is fetched
  straight from the turn. Telemetry that carries the token (`play.googleapis.com/log`) is not
  reachable from a turn, which runs without it. `/v1internal:writeTrajectoryAcls`, which
  names a conversation's owner on Google's side (its body is the conversation's ID alone),
  is forwarded. The gateway logs the routes it did not forward
  as `METHOD /path` only, the same route once per turn (never a token, a query, or a body).
- **GitHub Copilot specifics**: its login neither expires nor refreshes, so the gateway
  asks for a new login instead of refreshing a refused one. Copilot's hosted read-only
  GitHub MCP server (`/mcp/readonly`, which acts with the user's GitHub permissions) and
  the repository's custom agents are forwarded; its telemetry is not. Where the API of an account on another plan (Business,
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
  while it runs, spend the account's quota, and put data into allowed request bodies. The
  allowed API includes GitHub Copilot's GitHub MCP server (reading with the logged-in GitHub
  account's permissions) and Codex's connected apps (the services linked to the ChatGPT
  account). This
  does not protect against the host's memory or its administrator.
- **Limits of this scheme**: Codex turns send no analytics. A Claude Code turn cannot
  read the account's profile (`/api/oauth/profile`). Grok Build's usage cannot be read in a
  turn; it is read where the login is held. GitHub Copilot sends no telemetry, and where Business and Enterprise accounts go is
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

GitHub, Git, and SSH write credentials never reach a provider process: the
environment inherits none of the host's environment variables. The member's own
`{PERSON_ID}_GITHUB_ACCESS_TOKEN` / `_SLACK_BOT_TOKEN` / `_SLACK_APP_TOKEN`, the LLM
provider API keys (`OPENAI_API_KEY` and friends), and the helpers and sockets that hand
out a credential on demand (`GIT_ASKPASS`, `SSH_ASKPASS`, `SSH_AUTH_SOCK`) are all
absent inside. They are consumed in the GuildBotics process on the host, and the member
CLI loads its own from the OS keychain, so their absence changes nothing a member can
legitimately do.

Codex, Claude Code, Grok Build, GitHub Copilot, and Antigravity each receive the
command's HTTP MCP endpoint, which listens on the host's loopback and is reached from
the microVM as `host.microsandbox.internal`, and an unguessable bearer grant.
Grok Build and GitHub Copilot attach the endpoint through ACP `mcpServers`; Codex and
Claude Code receive process-local MCP configuration. Codex reads the raw token from a
dedicated environment variable through `bearer_token_env_var`, so Codex itself adds the
required `Authorization: Bearer` prefix. Antigravity reads `.agents/mcp_config.json`
from a private auxiliary workspace added with `--add-dir`; the process cwd and primary
workspace remain the member's actual working directory. The single
`guildbotics_member` tool accepts
only tokenized arguments for the fixed `guildbotics member` entrypoint; it cannot choose
an executable, invoke a shell, override the workspace, or act as another person. The
endpoint runs in the GuildBotics process on the host, outside the microVM, is usable only
while a turn is active, requires a second grant rotated on every turn, and is stopped
when the command ends. Provider processes never receive the member execution lease.

The broker runs the member CLI inside the trusted GuildBotics process on the host that
runs the command, where OS Keychain and other SecretStore backends remain available, instead of
starting a CLI process per call. Each command runs on a worker thread of its own with
its own working directory, standard streams, and invocation, so commands running at
once never see each other's. It acts under the execution lease the turn holds, in the
workspace that process has selected, and reads relative paths from the member's
isolated working directory. A command that outlasts the broker's timeout is reported to
the agent and left to finish. A read-only turn holds no lease, so the member CLI guard
rejects every write-capable command. Every native adapter uses this same member
capability boundary.

### Member git and the member's clones

The member's clones (`<workspace>/.guildbotics/local/clones/<person_id>`) are
written by the turns, and a repository decides what its git runs (hooks,
filters, `core.fsmonitor`), where it fetches from and pushes to, and which
directory it works on. So the member CLI never runs git in a clone on the host.
`member git prepare`, `commit`, `push`, and `publish` in member mode run every
git of the clone inside the running command's microVM, which the broker hands
each member command; that git starts with the facts of the host every
environment gets and the member's name, and with none of the turn's variables,
the broker's token, or a login stand-in.

The member's GitHub token is used only on the host, by a bare repository of the
host's own per `owner/repo` under
`<workspace>/.guildbotics/local/member_git/<person_id>/` that no environment
mounts, toward the URL the host derives. `prepare` records which of those a
clone pushes to (a fork and its upstream share one clone directory, and the
last `prepare` decides), and a clone `prepare` did not check out is not pushed.
History crosses between the two as git bundles streamed over the process's
standard streams, incrementally: the host takes of what the clone sends only
`refs/heads/<branch>` (a name git itself accepts as a branch name; at most
1 GiB), reads the commit it pushes back from its own repository, and pushes
with a full refspec. `prepare` rewrites the clone's `origin` URL, the branch's
upstream, and the member's `user.name` / `user.email` every time, and a push
moves the clone's `origin/<branch>` to what it pushed. A member git command
that outlasts the broker's timeout has its git in the microVM ended, and the
member's next git command waits for it.

What this means for a workspace: the project's git hooks run inside the
environment with the tools of its image (a hook that needs Python needs an
image that has it), Git LFS is not supported, and outside a running command
there is no environment, so member-mode git is refused there. An interactive
session uses `--workspace-mode current`, whose commit runs in the user's own
repository on the host, under the user's own hooks; a turn cannot use it. Its
push takes the branch's history into a repository of the host's own and pushes
from there toward the URL the host derives, as member mode does, so the
member's credential never enters the user's repository (whose configuration --
`pushurl`, `pushInsteadOf`, `core.sshCommand`, hooks -- would decide where it
goes). Where to push is still read from the user's `origin` as git resolves it,
and must be one repository of the configured owner; a `pre-push` hook does not
run. In both modes git's own configuration (system, global, the environment)
still applies -- a proxy, a CA, the TLS backend -- but unless git itself
resolves the host's repository's `origin` to exactly the URL the host derived,
for both fetch and push (an `insteadOf`, a `pushInsteadOf`, or a
`remote.origin` setting would move it), the credential is not sent and the
fetch or push is refused.

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
process failure, provider context compaction, or TTL/turn/usage limits. Model
and effort changes keep the provider session and are sent with the next turn.
Codex `contextCompaction` and Claude `compact_boundary` events are normalized to the
same runtime event; the completed turn remains successful, while the next dispatch
starts a new generation and rebuilds the Slack snapshot.

ACP has no standard compaction notification, so Grok compaction is normalized from
xAI's `auto_compact_*` extension notifications. Grok Build does not emit the standard
ACP `usage_update` at all (observed from 0.2.114 through 1.0.44), so the absolute
session context snapshot stays empty and the 90% `context_limit` rotation does not arm. The handling is
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
otherwise. Neither Grok Build (observed from 0.2.114 through 1.0.44) nor GitHub Copilot
CLI (observed from 1.0.77 through 1.0.89) advertises `sessionCapabilities.resume`, so both take the `session/load` path. `session/load` replays the whole transcript before it answers, so that
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
parallel. A workflow's member command writes only under the execution lease its turn
holds, and only as that lease's person.

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
`agy` reports quota and credential failures in that one string rather than in a
separate code field. A real quota exhaustion on 1.2.13 returned the plain message
`Individual quota reached. ... Resets in 25h57m34s.`. Responses with upstream status
and HTTP code prefixes (`UNAUTHENTICATED (code 401): ...`,
`API error (attempt 5): RESOURCE_EXHAUSTED (code 429): ...`) were also observed by
replacing the inference answer at the gateway. That one field is matched against
a small pattern set kept in the adapter. The recovery time it may carry is passed
through the same normalization every other tool uses. Joined units such as
`Resets in 25h57m34s` contribute all hours, minutes, and seconds to the recovery time.
A rate limit does not rotate the session;
authentication, protocol, and process failures do.

On 2026-10-02, a real quota exhaustion on 1.2.13 returned a terminal event in about
nine seconds despite an upstream 429 containing
`RetryInfo.retryDelay: "93454.995843114s"`. The captured
[fixture](../tests/guildbotics/intelligences/agent_runtime/fixtures/antigravity_quota_1_2_13.json)
tests classification, and the opt-in provider contract test replays that 429 and
requires termination within 30 seconds. Replay rebases only the absolute
`quotaResetTimeStamp` to the current time plus the captured `retryDelay`, keeping
the reset in the future as calendar time passes. The captured fixture stays unchanged.
This observation covers that quota
response; it does not establish a termination time for every 429 response.

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
verified baselines are Grok Build 1.0.44 and GitHub Copilot CLI 1.0.77 and 1.0.89. Antigravity
1.1.11 exposes the required flags, and 1.2.13 was observed loading the MCP configuration
from the added auxiliary workspace and calling the member broker through it.

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

GitHub Copilot CLI reports its session context (`used` / `size`) with the standard ACP
`usage_update` (observed on 1.0.86 and 1.0.89; 1.0.77 sent none), so the 90%
`context_limit` rotation arms for Copilot alongside the TTL and turn-count limits. It
reports no per-turn token counts -- neither `usage_update` nor a private extension
channel carries one -- so its input and output token counters stay at 0 and the
token-total limit does not arm. Copilot rate
limits are likewise classified only from structured RPC error data -- its weekly quota
identifier `user_weekly_rate_limited` among them -- and never from stderr text or
assistant prose; an error that cannot be classified becomes a protocol failure that
rotates the session.

GitHub Copilot's account quota comes from a different path than that ACP turn: the
Copilot SDK server protocol (JSON-RPC with `Content-Length` framing) that the same
CLI serves through `copilot --headless --stdio`. GuildBotics pins GitHub Copilot CLI
1.0.91 in the isolated environment (the 1.0.83 server had no `account.getQuota`).
After `connect`, `account.getQuota` answers with `quotaSnapshots`
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
context size (observed on 1.2.13), so context-usage rotation does not arm for Antigravity; only the TTL,
turn-count, and token-total limits do. This is the same situation as Grok.

Account quotas are a separate path from those per-turn counters. From Antigravity
CLI 1.1.11, `agy -p "/usage" --output-format json` answers the read-only `/usage`
slash command without starting an agent turn, spending quota, or leaving a
conversation. GuildBotics pins Antigravity CLI 1.2.16 in the isolated environment
and verified the structured payload on 1.2.13: `command.data.groups[].buckets[]`
carries each model group's `window` (`weekly` / `5h`), `remaining_fraction`, and
`reset_time`. Those buckets become the same usage meters the Activity view already
shows for Claude, Codex, and Grok. A `window` other than `weekly` / `5h` keeps
its raw value in the label so rows stay distinguishable; GuildBotics does not
guess a duration from the name. As with Claude and Codex, `limit_reached` is
true when any window is exhausted, so the member badge can light while another
model group still has quota. Accounts that do not expose quotas, and payloads
that are not that JSON, stay unavailable; GuildBotics does not parse the TUI,
the status line, or tab-separated text, and it does not synthesize 0% or 100%.
