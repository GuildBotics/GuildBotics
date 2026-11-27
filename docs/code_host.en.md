# Code Host and CI Guide

GuildBotics keeps a code host on the Hub. It provides Git repositories over Smart HTTP in
workspaces, pull requests with reviews and CI, and a task flow for each workspace's
tickets. GuildBotics uses it as the code host (`services.code_hosting_service`) and the
tickets (`services.ticket_service`), in place of GitHub. The chat stays whatever the
project chooses (Slack, for example).

This guide covers how to prepare and use the code host and CI. The server runs the code
host; its runner and data maintenance are `guildbotics runner` and `guildbotics server hub-data`
(see the [CLI reference](cli_reference.md)).

Ticket mention selection uses the `mentions` lists returned on bodies
and comments. Names are resolved against the members when a body is saved.
Patrols also read `GET /workspaces/{workspace}/tickets?mentions=me` to find
unanswered mentions outside the member's assignment, without scanning every
ticket's comments. These runs respond on the ticket and leave its lane and
assignee unchanged. Comments addressed only to other members do not wake the
assignee; a response that affects their work should mention them.

A ticket a human marks interactive (the ticket's `interactive`), and every
descendant of it (a ticket with an interactive ancestor, the ticket's
`interactive_ancestor`), is theirs to work on with an agent: the patrol never
selects it, by any trigger (the ready or working lane, a comment, or a mention,
assigned or not). A pull request that links it is still reviewed, but the member
does not take up its own such pull request, neither for feedback nor once the
checks it awaits settle.

## How it fits together

```text
Hub machine                                 another machine
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ guildbotics server start     │            │ OpenSSH (OS user service)    │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics and code host    │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- The code host runs once, on the Hub (the machine where GuildBotics runs), and listens on
  loopback only.
- Every machine reaches the GuildBotics server at the same base URL, `http://127.0.0.1:7878` by default.
  On the Hub machine that is the server itself. On another machine, set up the
  [SSH forwarding service](#set-up-ssh-forwarding-on-another-machine) once. OpenSSH
  starts at login and forwards the loopback ports over one connection, independently
  of the GuildBotics client. The browser, git and the GuildBotics CLI on that
  machine use it.
- The same base URL everywhere is what keeps recorded URLs valid: a ticket is
  `{base}/{repository}/-/tickets/{n}` and a pull request `{base}/{repository}/-/pulls/{n}`
  on every machine. A repository numbers its tickets (`#1`) apart from its pull requests (`!1`).

The GuildBotics server calls the code host directly, in its own process. While the embedded
service is not running, for example while the server stops, every ticket and pull request
operation fails, and **Settings → Verification** reports that the workspace cannot be read.

Such a failure is recorded as `ServiceUnavailable`. Autonomous patrols and
scheduled commands defer that work: they leave no failed workflow comment, record a started
run as `deferred` (shown as information in Activity), and preserve the worker's
consecutive-error count. Other work, including chat, keeps running. A later patrol checks
again and resumes work once the service runs. A manual command still returns its error.
Other failures remain failures; they are never silently retried as unavailable work.
An unavailable scheduled occurrence stays due and is retried on a later scheduler cycle.

## Prepare the code host

Follow the [GuildBotics installation steps](../README.md#installation) on the Hub:

1. Start the server. Its first start creates the code host's data (in `GUILDBOTICS_CODE_HOST_DATA_DIR`,
   by default `~/.guildbotics/data/code-host`) and the owner, the human member who uses
   the screens and merges pull requests. Set up SSH forwarding on the other machines as
   described below.
2. Create the repositories the members work in (`acme/demo`, with the labels its tickets
   may carry) under **Settings → Repositories**. A workspace (`acme`) holds repositories and has one board;
   a repository holds its tickets; a merge closes only tickets of the pull request's
   repository. You can create the workspace at the top of the sidebar (below).

Members are not created on the screens: GuildBotics creates each AI member ([Members](#members)).

## Install the Windows client

Uninstall the old GuildBotics Desktop application first. Keep human checkouts and SSH forwarding.

1. Set up SSH forwarding with `scripts/hub-forward.ps1` as described below.
2. On the Hub, run `guildbotics open --print` and open its entry link in the Windows browser.
3. In Settings → This machine, download `guildbotics.exe` using the Windows x86_64 button. The browser session authorizes the download of the Hub's current build.
4. In PowerShell, run `./guildbotics.exe install`. This installs the managed CLI in `~/.guildbotics/bin`, adds PATH, reports each AI tool's skill status, and configures human Git with the managed CLI's credential helper for the forwarded Hub origin. Open a new terminal after PATH changes. Resolve any warning about an earlier CLI on PATH.
5. The client-only installer lists existing registered workspaces' `.guildbotics` directories, the identity root, and exact keychain entries. Only typing `DELETE` removes them. Enter or EOF keeps them. General files and checkouts, human Git tokens (`GuildBotics/branvil-git`), consent tokens (`guildbotics-server`), device ID, managed CLI and SSH forwarding remain.
6. Run `guildbotics connect` and approve this machine in the browser. Re-run `guildbotics install` to check or refresh skills; user-edited skills are preserved. Subsequent CLI calls update to the Hub's build automatically.

## Install the Linux client

1. Set up the SSH forwarding user service below on the Linux x86_64 desktop.
2. On the Hub, run `guildbotics open --print` and open the entry link in the Linux browser.
3. In Settings → This machine, download `guildbotics` using the Linux x86_64 button.
4. From the download directory, run `chmod +x guildbotics` and `./guildbotics install`.
   Put the installed `~/.guildbotics/bin` on PATH if the installer prompts you to.
5. Run `guildbotics connect` and approve the client in the browser. Subsequent CLI
   calls update to the Hub's build automatically.

## Set up SSH forwarding on another machine

Run the bundled script from a checkout on each machine other than the Hub. It
needs only OpenSSH and the OS service manager; Python and the GuildBotics CLI are
not needed. No setup is needed on the Hub itself, beyond accepting your SSH key. The
GuildBotics [browser](../README.md#use-it-from-a-browser) screens and the code host share
port 7878. Forward that one port.

1. Connect once with `ssh my-hub` (replace `my-hub` with your SSH config alias or
   `[user@]host`) and accept the verified host key. Configure keys and hostnames
   in your existing OpenSSH configuration. Do **not** put `LocalForward` in that
   host entry: an ordinary `ssh my-hub` would then compete with the service.
2. Use an unencrypted key or an unlocked ssh-agent key. The unencrypted
   `~/.ssh/id_ed25519` is a common choice. The service uses
   `BatchMode=yes` and strict host-key checking, so it never prompts. On Windows,
   enable the OpenSSH client and, if used, the OpenSSH Authentication Agent service.
3. Install the service using the commands below, then inspect its status.

macOS (launchd user agent) and Linux (systemd user service):

```sh
sh scripts/hub-forward.sh install --hub my-hub --port 7878
sh scripts/hub-forward.sh status
# Replace the entire port list by running install again with every desired port.
sh scripts/hub-forward.sh uninstall
```

On Linux, a user service does not inherit your login shell's `SSH_AUTH_SOCK`.
Without `--agent-socket` the service disables agent use and needs an unencrypted
key. To use an agent, pass its **stable, absolute socket path**, for example:

```sh
sh scripts/hub-forward.sh install --hub my-hub --port 7878 --agent-socket "$SSH_AUTH_SOCK"
```

The script sets `SSH_AUTH_SOCK` in the service. If your agent uses a new socket
path after login, install again with the new path or configure a stable socket.
macOS uses the agent made available to launchd by the OS.

Windows (Task Scheduler, Windows PowerShell 5.1; no administrator account needed):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action install -Hub my-hub -Port "7878"
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action status
# Replace the entire port list by running install again with every desired port.
powershell -ExecutionPolicy Bypass -File scripts\hub-forward.ps1 -Action uninstall
```

The execution-policy override permits the unsigned script for this invocation.
The task runs only while you are logged in, without a visible console. A small
PowerShell loop, kept in the task's own arguments, retries ssh after 15 seconds,
including on failure; the task has no execution time limit. Replacing or removing
the task also ends its ssh. macOS stores the definition at
`~/Library/LaunchAgents/local.hub-forward.plist` (ssh errors in
`~/Library/Logs/hub-forward.log`); Linux uses
`${XDG_CONFIG_HOME:-~/.config}/systemd/user/hub-forward.service` (logs via
`journalctl --user -u hub-forward.service`). The scripts do not change SSH config,
keys or known_hosts. There is one service per user: install replaces its Hub and
complete port list; the integrated server needs only `--port 7878` on POSIX or `-Port "7878"` in PowerShell.

All forwards bind only `127.0.0.1` and target the same port on the Hub's
`127.0.0.1`. A dropped connection is detected by SSH keepalives and restarted.
Linux and Windows wait 15 seconds after ssh exits; macOS's `ThrottleInterval`
limits how often launchd starts it to once per 15 seconds. Status reports the OS process/task and its last result, not an
end-to-end health check of the server. On Windows, a running task may be between SSH
attempts. Open `http://127.0.0.1:7878` in the browser to check that the server is reached.

Do not run a second forwarding service for the same ports. The scripts themselves
contain no application names or default ports.

The Hub has one code host, used by every workspace. The members are the same in every
workspace.

The server uses one browser entry and session for all of its screens.
`guildbotics open` issues a link to `/enter`. The sidebar gives access to all screens.

## Set up the code host and the tickets

Choose or create a workspace at the top of the sidebar. Open **Settings → Repositories**
to manage its repositories. Members write only to those repositories and take tickets
from that workspace. Initial setup configures both built-in services automatically.

There is no connection step. The server calls the service in its own process directly, as
the owner and as each AI member. It keeps no token for this and hands none to a member, a
command, or an isolated environment.

Choose a workspace at the top of the sidebar to open its **Task flow** or **Pull requests**.

When configuring `team/project.yml` directly, use the following service settings.

```yaml
services:
  code_hosting_service:
    name: branvil
  ticket_service:
    name: branvil
```

Press **Validate settings** under **Settings → Verification**: it checks that the chosen
workspace exists.

## Members

An AI member needs nothing done on the code host after it is added.

- An AI member is created automatically, named after the member's ID and with the member's
  display name, when the server starts, when a workspace is selected, when a member is saved,
  and before any member's operation. Others can assign it work before it has ever acted. An existing agent
  whose name equals the member's ID is used as it is. When the member of that name is not
  an agent, the operation is refused.
- The human member is the owner (created at the server's first start).
- For a run in which a member pushes or fetches with git, the server issues that member a
  token (labelled `guildbotics-git`) and hands it to git with the member's ID. It is revoked
  when the run ends and releases the service. The token is kept in no store.
- Removing a member from a workspace, or deleting it, keeps its member on the code host:
  its history names it.

## Use the code host from your own checkouts

You, the human member, are the code host's owner, and you can make a code host repository the
`origin` of your own checkouts on the Hub. The code host's git asks for a member's name and
token. GuildBotics hands git yours, so git never asks for a name or a password:

1. Copy the credential helper configuration from **Settings → This machine → Git authentication**.
2. Open your global git configuration with `git config --global --edit` and paste the lines
   **at the end**:

   ```ini
   [credential "http://127.0.0.1:7878"]
   	helper =
   	helper = "!'/Users/you/.guildbotics/bin/guildbotics' git-credential"
   ```

   The empty `helper =` stops git from asking the helpers configured before it (the OS
   keychain, Git Credential Manager) for the code host's credentials. A helper configured after these lines would
   be asked to store the token, so keep them last.
3. `git fetch origin` and `git push origin <branch>` now work without a prompt.

On its first request, the helper asks the running GuildBotics server to issue the owner a token
for the requesting machine. A valid cached token is checked directly at its own origin and reused without asking
the GuildBotics API; issuance requires the server to be running. The token is labelled `git:<device name> (<last 8 characters of the device ID>)`
and kept in the machine's OS keychain only. It is never written to a URL, a git configuration,
your shell history, or a workspace file. You can revoke it under **Settings → Review and authentication →
owner → Tokens**. Once the code host no longer accepts the token, the next request issues a new one in
its place and revokes the machine's other `git:` tokens.

The token is a full owner token, with the owner's permissions. Revoking it does not lock the
helper out: the next request has the server issue another.

When the helper cannot hand a token (the server or the embedded service is not running, or
the keychain cannot be used), git stops and prints why, without asking for a password. When
the remote is not a service of GuildBotics on this machine, the helper says so, and git falls
back to asking for a name and a password. The members' own git never uses this helper.
GuildBotics hands member git the token lent for the run ([Members](#members)) and drops every
configured helper.

## How members work with it

The ticket workflow and pull request patrol read the workspace's board and conversations:

- A ticket has at most one assignee: the agent that works on it. Assigning another
  member replaces the assignee, so two members never take up the same ticket.
- Every ticket is in a lane of its workspace's board: a ticket a member creates goes to
  **New**.
- A ticket assigned to the member in the **Ready** lane of the workspace's board is
  taken up and moved to **In progress**; one in **In progress** is taken up again when
  someone else has spoken since the member last did, or an unanswered mention remains.
  These ticket conversations are picked up even with an open or merged linked pull
  request; an existing open pull request is reused. The member's latest failure or
  rate-limit status holds the ticket first. With no unanswered conversation, an open
  pull request is followed by the PR patrol, and a merged one moves the ticket to
  **Done**. Without a linked pull request or comments since assignment, work resumes.
- A member's pull request is taken up when someone says something the member has not
  answered; a pull request the member reviews, when a review is requested, when new
  commits land after its last review, or when there is a statement it has not answered.
  After a member has reviewed as many heads as the repository's limit (**Automatic
  re-review limit** on the CI tab of **Settings → Repositories**, 3 by default),
  automatic re-review not driven by a request
  stops when a head that member has never reviewed is pushed: the host ticket selector makes the
  pull request a draft during candidate selection, records it in Activity, and
  announces it on the PR and through the owner’s webhook in **Settings → Notifications**. Statements and thread replies on an already reviewed head still
  start a run after the limit, including heads restored by force push or reviewed
  before the count restarted. Explicit review requests remain outside the limit.
  Mark it ready for review to go on; every reviewer's count starts over.
- A member does not wait for CI inside its run: when only unfinished checks are left, it
  ends the run with a waiting notice on the pull request, and the patrol resumes it once
  the checks finish or new commits land.
- Draft pull requests are left alone. Only the owner makes a draft ready for review.
- A run that fails or is rate-limited leaves a status notice on the ticket or the pull
  request; the screens show it as a notice, and it holds the item until someone acts on it.

## Repositories with an upstream on GitHub

A repository on the Hub may follow a GitHub repository (its *upstream*). GitHub then stays the
place of record: its protected default branch, people's approvals and merges, its CI
(Windows included), releases, and the pull requests people outside see. The Hub is where the
members try things: many pushes, review rounds, and CI runs, locally and without cost. The Hub
never pushes GitHub's default branch; it follows it.

1. Give every agent a GitHub account: under **Settings → Review and authentication**, edit the
   member and open **Accounts on code hosts**. The account is checked with GitHub and publishes
   the pull requests that member authors. It is a GitHub App (its app ID, the ID of its
   installation on the account that owns the repositories, and its private key; it acts as
   `<app>[bot]`, and its short-lived tokens are made automatically) or a token of a bot account.
   One of them also reads GitHub, so your own token is not needed; set the owner's account only
   to publish pull requests you author yourself.
2. Point the repository at its GitHub repository under **Upstream** in its settings. An empty
   repository is filled with GitHub's default branch; an existing one must be the same or
   behind. The settings show whose account reads it and let you choose another.

From then on:

- Members work on the Hub exactly as above. Nothing reaches GitHub while they do.
- When a pull request is done, the owner uses **Publish to GitHub** where the merge button was.
  It shows exactly what will be posted (title, body, and `Closes #12` for linked tickets that
  name a GitHub issue), then pushes the branch and opens a pull request on GitHub with the
  final title and body as the author's GitHub account. Later pushes and edits follow.
- With **Land automatically** on in the repository's settings, a pull request is published by
  itself once it meets the conditions. Once the published head's GitHub checks succeed and no
  more can arrive, the webhook is told once per PR head, with links to both pull requests.
  Failed GitHub checks are reported in a comment. The owner enters the webhook URL under
  **Settings → Notifications**. For Slack, use an Incoming Webhook for a channel that no member
  watches. The URL is kept like a credential and never shown again. Creating a repair ticket
  after main verification fails sends one notice with the failed jobs, commit range, ticket URL
  and assignee to this webhook.
- GitHub's checks of the published head appear among the pull request's checks
  (`github/<name>`) with their logs, so a failure on GitHub (Windows, for example) keeps
  the pull request from being ready, and the member fixes it on the Hub; later pushes
  reach GitHub on their own.
- People approve and merge on GitHub. The Hub's repository then follows GitHub's default branch and
  marks its pull request merged, so the patrol moves the ticket to **Done** as usual.
- A ticket can name the GitHub issue it stands for in its **GitHub issue** field; the published
  pull request then closes that issue. Without it, nothing about the ticket is written on GitHub.
- The Hub's references in the title and body (`#3`, `acme/demo#3`, `!3`, their URLs) mean
  something else on GitHub, so publishing rewrites them: to the GitHub issue or pull request
  they stand for, or to a code span that GitHub does not link. The preview shows the result;
  when it still needs a hand, the owner edits the title and body in the publish panel.
- Conversation, reviews, threads, tickets, and boards stay on the Hub; back them up with
  `guildbotics server hub-data backup`.
- GitHub's Dependabot alerts of the upstream can be read
  (`member repo-info read --resource dependency_alerts`,
  `repository/security_alerts`). They are read with the account that reads GitHub and are
  not kept. A repository without an upstream has no alerts and refuses the read as unsupported.

The reading account needs the following permissions: for a GitHub App, Repository permissions;
for a token, the same permissions (fine-grained) or the scope in parentheses (classic). A
permission added to an installed GitHub App takes effect once the account that installed it
approves it.

- **Checks: Read-only** and **Actions: Read-only** (checks, workflow runs, and job logs; classic: `repo`)
- **Dependabot alerts: Read-only** (Dependabot alerts, enabled for the repository; classic: `security_events`)

### Initial ticket state

Ticket creation sends interactive mode, assignee, lane, parent and blockers in
one request. The server commits them together; patrol never observes a partially
configured ticket. Interactive tickets and their descendants are excluded from
patrol. Without an explicit lane, a ticket with a parent or blockers starts in
`ready`; other tickets start in `new`.

## Join another machine as a CI runner

Keep SSH forwarding running and install/connect the GuildBotics client first.
On Windows x86_64 or a Linux x86_64 desktop, run `guildbotics runner join` and
approve CI runner access in the browser. Linux requires glibc 2.36 or later, Git,
the libcap-ng runtime, an unlocked Secret Service keyring, and read/write access
to `/dev/kvm`. Keep the `hub-forward.service` user service running.

The token stays in the OS credential store. A Windows scheduled task or a
systemd user service starts at login and restarts the runner after it exits.
When the Hub changes version, the restarted client updates before requesting work.
On Linux, use `systemctl --user status guildbotics-runner-7878.service` with the
forwarded port to check the service. Rejoining revokes that device's previous
runner token. The runtime is downloaded from the pinned microsandbox release
and verified with SHA-256 and `msb --version`.

A joined runner becomes a runner member named `runner-<device ID>` (its
tokens are in Settings → Members). To tell runners apart, select
a runner in Settings → CI and give it a display name. The CI settings, checks,
and job pages show the display name; job details and the CI settings add the
name that `--runner` and the logs use in parentheses.
