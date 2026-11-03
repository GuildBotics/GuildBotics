# Branvil

Branvil is a lightweight local code hosting service for GuildBotics: Git
repositories over Smart HTTP in workspaces, each workspace with one kanban
board for the tickets of its repositories, pull requests with reviews and a
serial landing queue, and a CI that runs each job in a microVM. It is one Rust
process that keeps metadata in redb and Git data in bare repositories, and
listens on loopback only; CI jobs run in a separate `branvil runner` process.

Status: the service core (Git hosting, workspaces, tickets and boards, pull requests,
reviews, merge, recovery), CI (pipelines, queue, runner, microVM executor),
following an upstream on GitHub and publishing pull requests to it,
and backup/restore are implemented and tested. The GuildBotics `branvil`
provider lives in the same repository
(`guildbotics/integrations/branvil/`), which checks it against a
real service (see "Checking with GuildBotics"). See
[`docs/architecture.md`](docs/architecture.md) for what exists.

This crate of the GuildBotics repository is a snapshot of the Branvil
repository's `main` at commit `5f2728a840be25f3c54c82b92ee1c064be754949`. The
production Branvil still runs from a checkout of the Branvil repository with
that repository's `scripts/serve.sh` and `scripts/update.sh`, which this copy
does not carry; where the documents describe them, they describe that
repository's.

## Important Notice (Disclaimer)

- This software is in alpha. Its data format, HTTP API, configuration, and
  command line will change incompatibly without notice.
- The author and distributor assume no responsibility for malfunctions of this
  software or damages caused thereby. Use it at your own risk.

## Requirements

- Rust 1.90 or later (developed with 1.96, edition 2024)
- Git 2.38 or later on `PATH` (the service runs `git http-backend`,
  `update-ref`, `merge-tree --write-tree`, `diff`, …, and the CI runner checks
  the source out with it; developed with 2.54)

Verified on macOS 26 (arm64). Linux and Windows are not verified.

For CI: the microsandbox runtime `msb` 0.6.17 (with its `libkrunfw`) and a
hypervisor (macOS on Apple Silicon: built in; Linux: KVM; Windows: Windows
Hypervisor Platform, preview). GuildBotics deploys this runtime, and the runner
uses that copy; see [`docs/ci.md`](docs/ci.md).

## Build

```sh
cargo build --release
```

The binary is `target/release/branvil` in the Cargo workspace's root. The browser GUI from `web/dist` is
embedded when it exists at build time; without it the service runs and answers
web pages with a "GUI is not built" notice.

## First run

```sh
# Data directory, configuration (branvil.toml), and the owner: the one person
# who uses Branvil. The owner's token goes to ./data/secrets/owner.token.
branvil init --data-dir ./data --owner alice

# Start the service. Ctrl-C stops it: open GUI event streams end at once,
# other requests get 5 seconds to finish, and the stop waits for the
# repository operations already started (a merge, recording a push); Git
# never gets the Ctrl-C, and a second Ctrl-C does nothing. `kill -9` ends it
# at once and is safe: the next start waits for the Git left behind. SIGTERM
# does the same once the running CI jobs have finished (docs/ci.md,
# "Stopping the service").
branvil serve --data-dir ./data

# Open the GUI in the browser; there is no sign-in screen.
branvil open --data-dir ./data
```

Without `--data-dir` (or `BRANVIL_DATA_DIR`), the data directory is the OS
convention for `branvil` (macOS: `~/Library/Application Support/branvil`). The
configuration file is `<data dir>/branvil.toml` unless `--config` names
another. `branvil init` writes `listen = "127.0.0.1:7878"` and
`base_url = "http://127.0.0.1:7878"`; `--listen` and `--base-url` change them.
The base URL is permanent: every URL Branvil returns (tickets, pull requests,
clone URLs) is built from it, so keep it the same on every machine and over
time.

`branvil serve` prints `branvil listening on http://127.0.0.1:7878 (base URL …)`
once it accepts requests. It refuses non-loopback listen addresses.

Branvil has one human, the owner; everyone else is an agent (GuildBotics'
AI members) or a CI runner, each with its own token. Whoever can run commands
as the Hub machine's OS user is the owner: the owner's token is the `0600` file
`<data dir>/secrets/owner.token`, and the CLI on the Hub uses it by default.
`branvil open` asks the service for a single-use link (valid for 60 seconds)
and opens it with the system's browser; the GUI exchanges it for a session
cookie that lasts 30 days (`limits.session_hours`). `branvil open --print`
prints the link instead (for a headless Hub, or a Hub shared with other OS
users: the command line of the browser launcher is visible in `ps`). On other
machines, a local app such as GuildBotics Desktop gets an owner token of its
own once the owner allows it on Branvil's consent page (`/-/authorize`), and
opens the GUI with an entry link the same way (`docs/architecture.md`, "Local
apps"). There is no sign-out: to end every session, stop the service, delete
`secrets/owner.token`, and start it again (a new owner token is written and
the old one stops working; tokens given to local apps stay until revoked under
Settings → Members → owner → Tokens).

## Set up a project

The `member`, `workspace`, `repo`, `backup`, and `open` commands talk to
the running service. They take the base URL from the configuration (or `--url`
/ `BRANVIL_URL`) and a token from `--token`, `BRANVIL_TOKEN`, or else the
owner's token file of the data directory.

```sh
export BRANVIL_DATA_DIR=./data

branvil workspace create acme
branvil repo create acme/demo --label bug --label feature
branvil member create aiko --kind agent
branvil member token aiko          # prints aiko's token once
```

A workspace holds repositories (`acme/demo`: the workspace and a name) and
has one board. A repository holds its tickets and numbers them from 1 (`#1`),
apart from its pull requests (`!1`); a ticket carries only labels its
repository defines. Every ticket is on its workspace's board, in a lane: at
the end of New unless it is created in another lane. A merge closes only
tickets of the pull request's own repository. On the ticket page, a ticket
gets a parent ticket (it is not completed while a child is open) and tickets
it is blocked by (it is not moved to In progress while one of them is open),
among the tickets of its workspace.

On an open pull request, **Related tickets → Edit related tickets** adds
tickets from the same workspace, removes links, and chooses **Close on merge**
for each ticket of the PR's repository. Tickets of other repositories are
references only. Saving checks the revision the editor opened on; if someone
changed the PR meanwhile, cancel (discarding the unsaved edits), open the editor
again, and reapply the changes to the latest links. The editor shows known ticket
titles and marks closed tickets in the choices. Closed and merged PRs show the
related tickets section only when links exist.

The sidebar lists the task flow before the task board. **Settings → Display**
(under **All of Branvil**) can hide or show the task board's sidebar link (shown
by default). This preference is saved in the current browser and leaves direct
board URLs usable.
A workspace's page (`/{workspace}`) is its task flow; its board is at
`/{workspace}/-/board`. Opening `/` or creating a workspace always opens the
task flow. Switching workspaces keeps the task flow, pull requests, or settings
page, and keeps the board only when its sidebar link is shown. Switching from
a ticket or another page opens the task flow.

What a member may do follows from its kind. The owner may do everything. An
agent reads everything and works in every repository: tickets, comments,
reviews, pull requests, pushes to unprotected branches. Creating workspaces
and repositories, settings and upstreams, members and their accounts, backups,
merging, publishing, and marking a draft pull request ready are the owner's. A
merge or a publish needs a passed landing gate at the front of its base
branch's queue, the repository's required checks on the head, and its
required approvals (Settings → Repositories → a repository → CI; 1 unless
changed, 0 needs none): members other than the author whose last approve or
request changes approves the current head (a comment keeps it) and who were
not asked to review again since. The
author can only comment on their own pull request. With **Land automatically**
on the same tab, the repository merges (or, with an upstream, publishes once)
by itself an open pull request that is not a draft as soon as it meets them.
The queue and gates also run with this setting off. A pull request joins its
base branch's queue only once no statement awaits its author's answer: an
unresolved thread whose last comment is someone else's, a review with a body
(an approval may carry a suggestion), or a conversation comment (a machine's
notice, which carries `meta`, does not count; Branvil's own note of a failed
gate or host check does). The author answers with a reply in the thread, a
reaction, or anything said after it; a new statement takes a queued pull request
out, and the answer puts it at the end of the queue again. Pull requests the
owner wrote, and those of interactive tickets, never wait for this. Only the first pull request
in each repository/base queue gets a gate: the head itself when it contains
the base, otherwise a merge commit of base and head whose required checks
must succeed. The head and its approvals stay unchanged. The next pull request
waits until the first merges or leaves the queue, including while GitHub's
merge is pending. The pull request list and each pull request's page show
where it is on its way into the base and whose turn it is. Keeping a pull
request a draft holds it. A runner uses only the runner API.

Under Settings → Members → a member → Reviewers, the owner chooses who is
asked to review every pull request that member opens (owners and agents other
than the member). Changing them leaves pull requests already open as they are;
on a pull request's page the requests are changed one by one.

A ticket has at most one assignee: the agent that works on it. GuildBotics'
provider takes a ticket as an agent's work when that agent is its assignee, so
assigning another agent replaces the assignee instead of adding a second one.
The Interactive switch on a ticket's page, or Interactive in its card's menu
on the task board and task flow, marks it as worked on by the owner with an
agent at hand, and the board badges its card. GuildBotics' provider
then never takes up the ticket, nor the agent's own pull requests that link
it; reviewing those pull requests goes on as usual. The descendants of an
interactive ticket are interactive too, whatever their own switches say; their
cards' badges name that ticket.

GuildBotics' provider takes the pull requests involving an agent in the order
`GET /pulls?involves=me` returns them, one at a time, so Branvil puts first
what waits for least once that agent's work is done: a reviewer's work that
lets a pull request join the queue, then an author's work on an approved one,
and on down to approved pull requests that wait on no review
(`docs/api.md`). Between the same, a reviewer's work goes first: it takes
less time than an author's, which may need a fix.

## Git

Clone URL: `<base URL>/git/<repo>.git`, for example
`http://127.0.0.1:7878/git/acme/demo.git`. Fetch and push both use HTTP Basic:
the username is the member name, the password is one of the member's tokens.
There is no anonymous fetch. The owner uses their own member name and an owner
token, too. Keep tokens out of URLs, Git configuration, command arguments, and
shell history. On a machine other than the Hub, first keep a connection to the
Hub running as described in [Several machines](#several-machines).

The default branch is protected: it changes only through a merged pull
request. The first push of the default branch into an empty repository is
allowed for the owner.

### Your own checkout with GuildBotics

Connect GuildBotics to Branvil on the Hub and fetch that connection and its
credentials on the other machine. With GuildBotics running there:

1. Open **Setup → Integrations → Branvil → Your own git on this machine** in
   GuildBotics Desktop and copy the configuration it shows.
2. Run `git config --global --edit` and paste those lines **at the end**. For
   example, on Windows:

   ```ini
   [credential "http://127.0.0.1:7878"]
       helper =
       helper = "!guildbotics git-credential"
   ```

   Use the lines from Desktop for this machine's CLI path and Branvil base
   URL. The empty `helper =` resets earlier helpers for that URL; keep these
   lines last so another helper does not store the token.
3. In your existing checkout, set the remote and fetch (replace `acme/demo`
   with your repository; use `git remote add` if `origin` does not exist):

   ```sh
   git remote set-url origin http://127.0.0.1:7878/git/acme/demo.git
   git fetch origin
   ```

   After committing work on a feature branch, `git push origin <branch>`
   authenticates the same way, without asking for a username or password.

GuildBotics issues a separate owner token per machine on first use, labelled
`git:<device name> (<device ID suffix>)`, and keeps it only in that machine's
OS keychain. For connection setup, platform details, and troubleshooting, see
GuildBotics' [Use Branvil from your own checkouts](https://github.com/GuildBotics/GuildBotics/blob/main/docs/branvil_integration.en.md#use-branvil-from-your-own-checkouts)
([日本語](https://github.com/GuildBotics/GuildBotics/blob/main/docs/branvil_integration.ja.md)).

### Your own checkout without GuildBotics

Use an OS credential store and a separate token for each machine:

1. On the client machine, install or enable a Git helper backed by the OS
   credential store. Windows Git Credential Manager uses `manager`; macOS
   Keychain uses `osxkeychain`. On Linux, install a Secret Service helper such
   as `git-credential-libsecret` and have its keyring unlocked in your login
   session. Do not use `store`, which writes passwords in plaintext.
2. Run `git config --global --edit` on that machine and add this at the end
   (use your base URL). Choose the helper installed on that machine:

   ```ini
   [credential "http://127.0.0.1:7878"]
       helper =
       helper = manager
   ```

   Replace `manager` with `osxkeychain` on macOS, or with the installed
   `libsecret` helper's name or executable path on Linux. Keep these lines
   last so only this helper saves Branvil's credentials.

   If Git Credential Manager refuses the `http://` remote as unsafe, add
   `allowUnsafeRemotes = true` inside that same URL-specific credential
   section. Use this exception only for Branvil's loopback URL, with the
   OpenSSH forwarding to the Hub running; do not enable it for all remotes. See
   GCM's [unsafe remote URL settings](https://github.com/git-ecosystem/git-credential-manager/blob/main/docs/netconfig.md#unsafe-remote-urls).
3. On the Hub, with Branvil running, issue a token for this machine:

   ```sh
   branvil member token alice --label mini-win-git --data-dir /path/to/branvil-data
   ```

   Replace `alice` with the owner's member name, the label with a unique
   `<machine-name>-git`, and the data directory with the service's directory.
   The CLI reads the Hub's owner credential itself and prints the new token
   once. Run this in a private terminal without session recording; transfer
   it to the client through a trusted private channel, not a command line,
   file, or shared message.
4. Set `origin` and run `git fetch origin` as above. At Git's credential
   prompt, enter the owner's member name as the username and paste the newly
   issued token as the password. Enter it only in the password field, never
   at a shell prompt. After a successful fetch, the helper saves it in the OS
   credential store; clear the clipboard and the issuing terminal's
   scrollback. A second fetch and `git push origin <branch>` should use the
   saved credential without prompting. Push a feature branch with a commit
   when checking write access.

### Retiring a machine

Under Branvil's **Settings → Members → owner → Tokens**, revoke that machine's
`<machine-name>-git` token (or its `git:` token when using GuildBotics), leaving
the other machines' tokens in place. Remove the saved credential from the
retired machine's OS credential store as well, if you still have access.

A connected GuildBotics can issue a new token after revocation. To cut that
machine off, first remove it from the GuildBotics Hub, revoke `app:GuildBotics`
and reconnect GuildBotics on a retained machine, let the retained machines
fetch the new connection, then revoke the retired machine's `git:` token.
The linked GuildBotics guide describes this procedure too.

For automation, give Git credentials through a helper or `GIT_ASKPASS`. The
tests in `tests/` use an askpass script that reads the name and token from
environment variables.

## Several machines

Branvil runs once, on the GuildBotics Hub machine, listening on loopback. Every
machine uses the same base URL (`http://127.0.0.1:7878`), so URLs recorded in
GuildBotics work everywhere. On each other machine, use OpenSSH to forward
that loopback port over the SSH connection you already use for the Hub; no
Branvil binary is needed there:

```sh
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o BatchMode=yes -L 127.0.0.1:7878:127.0.0.1:7878 -- hub
```

Replace `hub` with a Host from `~/.ssh/config` or `[user@]host`. Connect once
with `ssh hub` and accept the verified host key before setting up the service.
`BatchMode=yes` means ssh never prompts: use an unencrypted key or an unlocked
key in `ssh-agent` available to the service. Keys, host keys, and host names
belong to OpenSSH; Branvil stores none of them. Keep both ports equal to the
base URL's port: Branvil refuses other `Host` ports. Do not add `LocalForward`
for these ports to the host entry: ordinary SSH sessions would compete for
them. To forward another Hub service, add another `-L` to this same connection.

Authenticate Git separately using
[Your own checkout with GuildBotics](#your-own-checkout-with-guildbotics) or the
[OS credential store procedure](#your-own-checkout-without-guildbotics).

The `branvil tunnel` command has been removed. Before updating an older client
CLI, stop and disable that command or its service, then replace it with the
service below. Run only one forwarding service for the ports on each client.
When forwarding is down, clients get a connection error; the Hub service and
its data are unaffected.

If you registered the old README's examples, retire them on the client with
the commands for your OS below. Adjust the names and paths if you customized
them. For a command started in a terminal, stop it with Ctrl-C instead.

```sh
# macOS: unload, then remove the login definition so it stays stopped.
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.guildbotics.branvil-tunnel.plist
rm ~/Library/LaunchAgents/com.guildbotics.branvil-tunnel.plist
```

```sh
# Linux: stop, disable login startup, and remove the old unit.
systemctl --user disable --now branvil-tunnel.service
rm ~/.config/systemd/user/branvil-tunnel.service
systemctl --user daemon-reload
```

```powershell
# Windows: disable retries/startup, stop the running task, then unregister it.
Disable-ScheduledTask -TaskName "Branvil tunnel"
Stop-ScheduledTask -TaskName "Branvil tunnel"
Unregister-ScheduledTask -TaskName "Branvil tunnel" -Confirm:$false
```

On Windows, stopping the old task may leave its child `ssh.exe` running.
Before starting the replacement service, inspect the SSH processes and the
port's listeners (use your base URL's port if it is not 7878):

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" |
    Format-List ProcessId, ParentProcessId, CommandLine
netstat -ano | findstr "127.0.0.1:7878"
```

Check the command line's Hub destination and `-L` ports to identify the old
forward; leave unrelated SSH processes running. Include an old SSH process
still connecting, even if it has no `LISTENING` row yet. For each verified old
forward, stop just that process using its `ProcessId` in the same PowerShell
window:

```powershell
$oldForwardPid = [int](Read-Host "ProcessId of the verified old SSH forward")
Stop-Process -Id $oldForwardPid
```

Repeat the process and listener checks until no old forward remains, then
start the replacement. Windows OpenSSH can bind the same port more than once,
so a successful new forward alone does not show that the old one has stopped.

### Keeping forwarding running

OpenSSH maintains the connection; the OS service starts it at login and
restarts it when it exits. This works independently of Branvil's CLI and
GuildBotics Desktop.

With GuildBotics, its bundled scripts register this OS service. From a
GuildBotics checkout on the client:

```sh
# macOS / Linux
sh scripts/hub-forward.sh install --hub hub --port 7878
sh scripts/hub-forward.sh status
```

```powershell
# Windows (Task Scheduler)
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action install -Hub hub -Port 7878
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action status
```

These scripts need only OpenSSH and the OS service manager. For multiple Hub
services, supply the complete port list in one install: repeat `--port` on
macOS/Linux, or use `-Port "7878,12345"` on Windows (replace 12345 with the
other service's actual port). On Linux, an agent needs a stable absolute
socket path passed with `--agent-socket`; without it the script disables agent
use and requires an unencrypted key. See GuildBotics'
[SSH forwarding setup](https://github.com/GuildBotics/GuildBotics/blob/main/docs/branvil_integration.en.md#set-up-ssh-forwarding-on-another-machine)
([日本語](https://github.com/GuildBotics/GuildBotics/blob/main/docs/branvil_integration.ja.md)).

Without those scripts, use the following manual setups instead. These examples
have **not been executed** on the target systems; adjust the SSH executable
path and destination. They run while the user is logged in.

macOS (launchd): create `~/Library/LaunchAgents` and `~/Library/Logs` if needed
and save `~/Library/LaunchAgents/local.hub-forward.plist`. Replace `/Users/you`
in `StandardErrorPath` with your absolute home path; plist values do not expand
`~` or `$HOME`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.hub-forward</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-o</string><string>ExitOnForwardFailure=yes</string>
    <string>-o</string><string>ServerAliveInterval=15</string>
    <string>-o</string><string>BatchMode=yes</string>
    <string>-L</string><string>127.0.0.1:7878:127.0.0.1:7878</string>
    <string>--</string>
    <string>hub</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/hub-forward.log</string>
</dict>
</plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.hub-forward.plist
launchctl print gui/$(id -u)/local.hub-forward
tail -n 50 ~/Library/Logs/hub-forward.log
# Stop and unregister:
# launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.hub-forward.plist
# rm ~/Library/LaunchAgents/local.hub-forward.plist
```

Linux (systemd user unit): create `~/.config/systemd/user` if needed and save
`~/.config/systemd/user/hub-forward.service`:

```ini
[Unit]
Description=OpenSSH forwarding to the Hub
After=network-online.target
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/bin/ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o BatchMode=yes -L 127.0.0.1:7878:127.0.0.1:7878 -- hub
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now hub-forward.service
systemctl --user status hub-forward.service
journalctl --user -u hub-forward.service
# Stop and unregister:
# systemctl --user disable --now hub-forward.service
# rm ~/.config/systemd/user/hub-forward.service
# systemctl --user daemon-reload
```

The unit needs an unencrypted key or an agent. A user unit does not inherit
the login shell's `SSH_AUTH_SOCK`: for an agent, add
`Environment=SSH_AUTH_SOCK=/absolute/stable/agent.sock` under `[Service]`, using
your agent's actual stable socket path, then reload and restart the unit.
macOS uses the agent available to launchd. If forwarding must continue without
a login session on Linux, enable lingering with `loginctl enable-linger "$USER"`.

Windows (Task Scheduler): enable the Windows OpenSSH Client, then create a
task named **Hub forwarding** under your own user account:

1. **General:** run only when this user is logged on; no elevated privileges.
2. **Triggers:** at logon of this user. Under advanced settings, repeat every
   **1 minute**, indefinitely, with the trigger enabled.
3. **Actions:** start `C:\Windows\System32\OpenSSH\ssh.exe`, with arguments
   `-N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o BatchMode=yes -L 127.0.0.1:7878:127.0.0.1:7878 -- hub`.
4. **Conditions:** clear the AC-power-only and stop-on-battery conditions if
   forwarding should work on battery too.
5. **Settings:** clear the execution time limit; for an already running task,
   choose **Do not start a new instance**. The repeated trigger retries after
   any exit, including a successful exit, without opening a second connection.
6. Sign out and back in to activate the repeating logon trigger. **Run** can
   test the connection immediately, but does not activate the trigger's repeat
   schedule. Inspect **Status**, **Last Run Result**, and task history in Task
   Scheduler. To stop forwarding, disable the task and end its running instance;
   delete it to unregister it.

Use an unencrypted key, or enable the OpenSSH Authentication Agent service
and load the key before starting the task. This manual task may show a console;
GuildBotics' script installs a task that runs without one.

Service status shows the SSH process, not Branvil's health. After setup, open
`http://127.0.0.1:7878` in a browser and run `git fetch origin` in your own
checkout with the authentication above. If the port is already occupied, stop
the other forward before retrying. Inspect the OS service's status when the
connection drops; SSH keepalives detect a lost connection and the service
starts another one.

## CI

Put the pipeline in the repository as `.branvil/ci.toml`. Every commit runs
its own copy, so a pull request that changes it runs the changed pipeline as
its checks; review that change like any other part of the diff:

```toml
[[jobs]]
name = "test"
image = "docker.io/library/alpine:3.22"
commands = ["./scripts/test.sh"]
timeout_secs = 600
artifacts = [{ name = "report", paths = ["out/"] }]
```

Then require the job for merging (`PATCH /api/v1/repos/{repo}`
`{"required_checks": ["test"]}`) and start a runner. The runner is a separate
process; it needs a runner member's token and the images in its cache (jobs
never pull; which version of a tag runs is the one the cache holds, so pulling
or loading the tag again moves the jobs to its new version):

```sh
branvil runner register runner1                                # on the Hub: prints runner1's token once
branvil runner pull docker.io/library/alpine:3.22
branvil runner doctor                                          # msb found? executor ready?
BRANVIL_TOKEN=<runner token> branvil runner                    # runs until stopped
```

Or let the service start and supervise one: under Settings → CI in the GUI
the owner switches the runner on, sets the job resources, and pulls an image or
loads one from a `docker save` archive on the Branvil machine (the page shows
the tag to write as `image`); a repository's CI tab allows its jobs the
network. The same settings are `runner = "auto"` and `[ci.executor]` in
`branvil.toml`.

Each job runs in its own microVM as a user that is not root (uid 1000), with
a Git checkout of the commit (its history and tags, no remote) mounted at
`/work`, `/tmp` on a disk of its own rather than in memory, no network (the
owner may allow public destinations per repository with `ci_network`), the
CPUs and memory of the runner's `[ci.executor]` settings, and its
`timeout_secs` capped by the runner's time limit (which is the limit of a
job that gives none). Without a runner, jobs end
`startup_failure` after two minutes; nothing ever runs on the host. Logs,
artifacts, retry and cancel, states, limits, and the trust model:
[`docs/ci.md`](docs/ci.md).

### This repository's pipeline

The GuildBotics repository's [`.branvil/ci.toml`](../../.branvil/ci.toml) runs
the checks of "Development": `rust-fmt`, `rust-clippy`, and `rust-test` for
the Cargo workspace, `rust-duplicates` (jscpd), `branvil-web` (`npm ci` at the
repository root, then `npm run lint`, `npm run typecheck`, and `npm run test`
in `crates/branvil/web`), and
`branvil-integration` (see "Checking with GuildBotics"). Their image is
GuildBotics' development image
(`guildbotics/agent-environment:dev`, built by
`scripts/build-agent-environment-image.sh`): the owner loads its `docker save`
archive under Settings → CI, and the pipeline names it by its tag. When the
image is rebuilt, loading the new archive is all it takes (the script
does so itself); the pipeline stays as it is. The Rust jobs fetch crates
from crates.io and `branvil-web` fetches packages from `registry.npmjs.org`, so the
repository's CI tab must allow its jobs the network. `rust-clippy` and `rust-test` cache
Cargo's registry and `target`, keyed by `Cargo.lock`; `branvil-web` caches npm's
downloads, keyed by the repository root's `package-lock.json`.

## Upstream (GitHub)

Branvil is where agents try things — many pushes, review rounds, and CI runs,
locally and without cost. A repository can have an **upstream** on GitHub that
stays the place of record: its protected default branch, people's approvals
and merges, its CI (Windows included), releases, and the pull requests people
outside see. Branvil never pushes the default branch; it follows it.

1. Give every agent a GitHub account in Branvil, checked with GitHub: the
   account that publishes its pull requests. It is a GitHub App (its app ID,
   the ID of its installation on the account that owns the repositories, and
   its private key; it acts as `<app>[bot]`, and Branvil makes its short-lived
   tokens itself) or a token of a bot account. One of them also reads GitHub
   for Branvil, so your own token is not needed (set it only to publish pull
   requests you author yourself):

   ```sh
   branvil member account aiko --app-id 123456 --installation-id 7890123 --private-key-file aiko-app.private-key.pem
   KEN_GITHUB_TOKEN=... branvil member account ken --token-env KEN_GITHUB_TOKEN
   ```

   Settings → Members → a member → Accounts on code hosts does the same.

2. Point the repository at its GitHub repository. An empty repository is filled
   with GitHub's default branch; an existing one must be the same or behind:

   ```sh
   branvil repo create acme/demo
   branvil repo upstream acme/demo --repository acme/demo
   ```

   The repository settings show whose account reads it; `--reader <member>`
   (or the settings) chooses another.

3. Agents work in Branvil as before. On a pull request the owner finds
   **Publish to GitHub** where the merge button was: it shows exactly what will
   be posted (title, body, and `Closes #12` for linked tickets that name a
   GitHub issue), then pushes the branch and opens the pull request on GitHub
   as the author's account. GitHub's checks of the head appear among the pull
   request's checks (`github/<name>`, with their logs), later pushes and edits
   follow, and once a person merges on GitHub, Branvil's default branch follows
   and its pull request and tickets close.

   With **Land automatically** on, Branvil publishes a pull request by itself
   once it meets the conditions. Each time its landing gate passes, Branvil
   tells a webhook, with links to both pull requests, that it is ready to
   merge on GitHub once GitHub's checks of its head have succeeded. This
   includes a new gate for an already published head. When a check on GitHub
   fails, Branvil says so on its pull request instead. Under
   Settings → Notifications, the owner enters its URL (a Slack Incoming Webhook takes the
   message as it is), which is kept like a credential and never shown again.

Members read the GitHub repository's Dependabot alerts through Branvil
(`docs/api.md`, "Dependency alerts"), with the reader's account; Branvil
keeps none of them. For that the reader needs Dependabot alerts enabled for
the repository and, for a GitHub App, Repository permissions → **Dependabot
alerts: Read-only** with access to the repository (adding a permission to an
installed app takes effect once the account that installed it approves it);
for a token, the same permission (fine-grained) or the `security_events`
scope (classic).

Branvil polls GitHub with conditional requests (an unchanged answer is a free
`304`): every 60 s while a published pull request is open, every 15 min
otherwise, and at once after publishing or "Check now". GitHub registers the
checks of a pushed head a little later, so a head waits for more checks until
`checks_registration_secs` after the push (or the pull request's creation,
edit, or reopening there) and while a workflow run of it has not finished.
The registration wait defaults to 60 s. The poller also wakes at its deadline
to read GitHub, without waiting for the next regular poll; only a successful
read ends the registration wait.
The reading account needs, for a GitHub App, Repository permissions →
**Checks: Read-only** and **Actions: Read-only** (the workflow runs and the
jobs' logs); for a token, the same permissions (fine-grained) or the `repo`
scope (classic).
Settings in `branvil.toml`:

```toml
[upstream]
poll_active_secs = 60
poll_idle_secs = 900
checks_registration_secs = 60
```

Notes:

- Conversation, reviews, threads, tickets, and boards stay in Branvil; back
  them up with `branvil backup`.
- A ticket names the GitHub issue it stands for in its "GitHub issue" field
  (`upstream_issue` in the API); without it nothing about the ticket is written
  on GitHub.
- Branvil's references in the title and body (`#3`, `acme/demo#3`, `!3`, its
  URLs) mean something else on GitHub, so publishing rewrites them: to the
  GitHub issue or pull request they stand for, or to a code span that GitHub
  does not link. The preview shows the result; when it still needs a hand, the
  owner edits the title and body in the publish panel.
- Real GitHub was not used for this build; the tests use a stand-in that
  answers GitHub's REST API, with bare repositories as its Git side.
- Design: [`docs/architecture.md`](docs/architecture.md) ("Upstream"); API:
  [`docs/api.md`](docs/api.md) ("Upstream").

## Backups

```sh
branvil backup --to /srv/backups/branvil-2026-10-14     # on the Hub, service running
branvil restore --from /srv/backups/branvil-2026-10-14 --data-dir /srv/branvil-restored
branvil admin check --data-dir /srv/branvil-restored
```

A backup holds the database, every repository (as a Git bundle), CI logs and
artifacts, the configuration, and a manifest with checksums. It is taken while
the service runs, with ref changes paused for the moment the database is copied
and the repositories are bundled. Members' upstream tokens are not in it,
nor the owner's token: the restored service writes a new one at its first start
and starts with no GUI sessions.
See [`docs/backup-restore.md`](docs/backup-restore.md).

## Maintenance

With the service stopped:

```sh
branvil admin check --data-dir ./data     # redb structure + every index against the records
branvil admin reindex --data-dir ./data   # rebuild every index from the records
branvil admin upgrade --data-dir ./data   # after updating Branvil, when the service says so
```

A build whose stored format changed refuses the data directory of the version
before it until `branvil admin upgrade` upgrades it; take a backup first. An
upgraded database is then refused by `branvil serve` until
`branvil admin check` passes. See "Upgrading" in
[`docs/backup-restore.md`](docs/backup-restore.md).

Logging goes to stderr; `BRANVIL_LOG` takes a `tracing` filter
(default `branvil=info`).

## Development

The repository's `scripts/dev.sh` builds the GUI and runs Branvil, on data of
its own, inside the server of the checkout (`guildbotics server start`), and
opens the GUI; see "開発用の起動" in the repository's `AGENTS.md`.

To run the Rust checks:

```sh
cargo fmt --all
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo nextest run --locked --workspace
```

The tests run with [cargo-nextest](https://nexte.st/), each in a process of
its own (`cargo install --locked cargo-nextest`).

To run the GUI checks (lint, types, and the Vitest tests):

```sh
npm ci --prefix ../..
cd web
npm run lint
npm run typecheck
npm run test
```

The tests use at most four workers and write file and test timings to
`web/test-results/timing.json`; CI keeps this in the `web-tests` artifact
and streams Vitest output into the job log. Measurements and interpretation
are in
[`docs/performance.md`](docs/performance.md#web-tests).

The integration tests start real `branvil serve` processes on free loopback
ports and drive them with real Git clients isolated from the user's Git
configuration and credential helpers. They are one test crate,
[`tests/main.rs`](tests/main.rs), with a module per file:
`cargo nextest run -p branvil --test main tickets::` runs the tests of `tests/tickets.rs`.

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

GuildBotics' `branvil` provider is this API's main client. Its port contracts
(`tests/guildbotics/integrations/contracts/` of the repository) run against a
Python double of the API; the repository's `scripts/branvil-integration.sh`
checks that the production provider and a real `branvil serve` still work
together:

```sh
# from the repository's root, environment synced (uv sync --extra test --extra dev)
scripts/branvil-integration.sh
```

It builds the debug binary, and walks
three paths, each against its own service (temporary data directory, free
loopback port, members and credentials of its own) through the provider that
GuildBotics' factory makes:

- tickets: created, changed, and read back with their assignee and lane;
  a parent and a ticket it is blocked by read back from both sides and are
  taken back; a ready ticket blocked by an open one is no patrol candidate and
  cannot be moved in progress until its blocker is closed;
- Git, pull request, and merge: a push with the provider's clone URL and
  credential, a pull request that closes a ticket, and, after the owner's
  merge, the pull request merged and the ticket closed in the done lane;
- checks: a pull request is not ready while its head's job is pending, is
  ready once that commit's job succeeds, and is not ready again when its head
  moves on.

Only the owner's setup and merge and the runner's claim and finish call the
API directly; every result is read through the provider. It needs no microVM,
browser, or running service, and touches nothing of this machine's GuildBotics
or Branvil ([`integration/branvil/test_smoke.py`](../../integration/branvil/test_smoke.py)).

Run it when you change the HTTP API or the provider. The pipeline runs it as
the job `branvil-integration` with the same commit.

## Documents

- [`docs/api.md`](docs/api.md): the HTTP API contract
- [`docs/port-mapping.md`](docs/port-mapping.md): GuildBotics port operations → API
- [`docs/storage-design.md`](docs/storage-design.md): tables, keys, indexes, transactions
- [`docs/architecture.md`](docs/architecture.md): modules, ref-change coordination, recovery, CI, upstream, backups
- [`docs/ci.md`](docs/ci.md): pipelines, trust model, states, limits, runner, executor and its prerequisites
- [`docs/performance.md`](docs/performance.md): measurements
- [`docs/security.md`](docs/security.md): threat model, authentication, Git, secrets, upstream tokens, what is not protected
- [`docs/backup-restore.md`](docs/backup-restore.md): backup contents, consistency, restore, credentials
