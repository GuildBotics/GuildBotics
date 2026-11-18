# Branvil Integration Guide

Branvil is a code host and board you run yourself: Git repositories over Smart HTTP in
workspaces, pull requests with reviews and CI, and each repository's tickets on its
workspace's board. GuildBotics uses it through the `branvil` provider as the code host
(`services.code_hosting_service`) and the tickets (`services.ticket_service`), in place of
GitHub. The chat stays whatever the
project chooses (Slack, for example).

This guide covers what GuildBotics needs of Branvil. The server runs it; its runner and
data maintenance are `guildbotics runner` and `guildbotics server hub-data` (see the
[CLI reference](cli_reference.md)).

Ticket mention selection uses the `mentions` lists Branvil returns on bodies
and comments. Branvil resolves names against its members when a body is saved.
Patrols also read `GET /workspaces/{workspace}/tickets?mentions=me` to find
unanswered mentions outside the member's assignment, without scanning every
ticket's comments. These runs respond on the ticket and leave its lane and
assignee unchanged. Comments addressed only to other members do not wake the
assignee; a response that affects their work should mention them.

A ticket a human marks interactive (`interactive` on Branvil's tickets), and every
descendant of it (a ticket with an interactive ancestor, Branvil's
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
│ GuildBotics and Branvil      │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil runs once, on the Hub (the machine where GuildBotics runs), and listens on
  loopback only.
- Every machine reaches it at the same base URL, `http://127.0.0.1:7878` by default.
  On the Hub machine that is Branvil itself. On another machine, set up the
  [SSH forwarding service](#set-up-ssh-forwarding-on-another-machine) once. OpenSSH
  starts at login and forwards the loopback ports over one connection, independently
  of GuildBotics and Branvil's client. The browser, git and Branvil's CLI on that
  machine use it.
- The same base URL everywhere is what keeps recorded URLs valid: a ticket is
  `{base}/{repository}/-/tickets/{n}` and a pull request `{base}/{repository}/-/pulls/{n}`
  on every machine. A repository numbers its tickets (`#1`) apart from its pull requests (`!1`).

The GuildBotics server calls the service directly, in its own process. While the embedded
service is not running, for example while the server stops, every ticket and pull request
operation fails, and **Setup → Verification** reports that the workspace cannot be read.

The provider then raises the port's `ServiceUnavailable` type. Autonomous patrols and
scheduled commands defer that work: they leave no failed workflow comment, record a started
run as `deferred` (shown as information in Activity), and preserve the worker's
consecutive-error count. Other work, including chat, keeps running. A later patrol checks
again and resumes work once the service runs. A manual command still returns its error.
Other failures remain failures; they are never silently retried as unavailable work.
An unavailable scheduled occurrence stays due and is retried on a later scheduler cycle.

## Prepare Branvil

Follow the [GuildBotics installation steps](../README.md#installation) on the Hub:

1. Start the server. Its first start creates the code host's data (in `BRANVIL_DATA_DIR`,
   by default `~/.guildbotics/data/code-host`) and the owner, the human member who uses
   the screens and merges pull requests. Set up SSH forwarding on the other machines as
   described below.
2. Create the repositories the members work in (`acme/demo`, with the labels its tickets
   may carry) in Branvil's GUI. A workspace (`acme`) holds repositories and has one board;
   a repository holds its tickets; a merge closes only tickets of the pull request's
   repository. GuildBotics can create the workspace itself (below).

Members are not created on the screens: GuildBotics creates each AI member ([Members](#members)).

## Install the Windows client

Uninstall the old GuildBotics Desktop application first. Keep human checkouts and SSH forwarding.

1. Set up SSH forwarding with `scripts/hub-forward.ps1` as described below.
2. On the Hub, run `guildbotics open --print` and open its entry link in the Windows browser.
3. In Settings → This machine, download `guildbotics.exe` from the Windows client card. The browser session authorizes the download of the Hub's current build.
4. In PowerShell, run `./guildbotics.exe install`. This installs the managed CLI in `~/.guildbotics/bin`, adds PATH, reports each AI tool's skill status, and configures human Git with the managed CLI's credential helper for the forwarded Hub origin. Open a new terminal after PATH changes. Resolve any warning about an earlier CLI on PATH.
5. The client-only installer lists existing registered workspaces' `.guildbotics` directories, the identity root, and exact keychain entries. Only typing `DELETE` removes them. Enter or EOF keeps them. General files and checkouts, human Git tokens (`GuildBotics/branvil-git`), consent tokens (`guildbotics-server`), device ID, managed CLI and SSH forwarding remain.
6. Run `guildbotics connect` and approve this machine in the browser. Re-run `guildbotics install` to check or refresh skills; user-edited skills are preserved. Subsequent CLI calls update to the Hub's build automatically.

## Set up SSH forwarding on another machine

Run the bundled script from a checkout on each machine other than the Hub. It
needs only OpenSSH and the OS service manager; Python and the GuildBotics CLI are
not needed. No setup is needed on the Hub itself, beyond accepting your SSH key. Both services share port 7878, including the GuildBotics
[browser](../README.md#use-it-from-a-browser). Forward that one port.

1. Stop any `branvil tunnel` you run manually or as a service.
2. Connect once with `ssh my-hub` (replace `my-hub` with your SSH config alias or
   `[user@]host`) and accept the verified host key. Configure keys and hostnames
   in your existing OpenSSH configuration. Do **not** put `LocalForward` in that
   host entry: an ordinary `ssh my-hub` would then compete with the service.
3. Use an unencrypted key or an unlocked ssh-agent key. The unencrypted
   `~/.ssh/id_ed25519` is a common choice. The service uses
   `BatchMode=yes` and strict host-key checking, so it never prompts. On Windows,
   enable the OpenSSH client and, if used, the OpenSSH Authentication Agent service.
4. Install the service using the commands below, then inspect its status.

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
end-to-end health check of Branvil. On Windows, a running task may be between SSH
attempts. Check `http://127.0.0.1:7878` in the browser to verify Branvil too.

Do not run a second forwarding service for the same ports. The scripts themselves
contain no application names or default ports.

Several GuildBotics workspaces can use one Branvil. The members are the same in every
workspace, and each GuildBotics workspace chooses the code host's workspace its members work in.

The server uses one browser entry and session for all of its screens.
`guildbotics open` and `guildbotics open --branvil` issue links to
`/-/guildbotics/-/enter`; only the landing screen differs.

## Set up the code host and the tickets

On the Hub's screens, under **Setup → Integrations**:

1. Choose **Branvil** for the code host and for the tickets. Both use the same service on
   the Hub.
2. Choose the **Workspace** the members work in, or create one there. Members write only
   to its repositories and take tickets from its board; reads are not limited.

There is no connection step. The server calls the service in its own process directly, as
the owner and as each AI member. It keeps no token for this and hands none to a member, a
command, or an isolated environment.

The item below **Setup** in the sidebar switches to the code host's screens in the same
browser session. It appears while the server runs with the code host embedded.

Without the GUI, the same settings are in `team/project.yml`:

```yaml
services:
  code_hosting_service:
    name: branvil
    workspace: acme
  ticket_service:
    name: branvil
    workspace: acme
```

Press **Validate settings** under **Setup → Verification**: it checks that the chosen
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

## Use Branvil from your own checkouts

You, the human member, are Branvil's owner, and you can make Branvil the `origin` of your own
checkouts on the Hub. Branvil's git asks for a member's name and
token. GuildBotics hands git yours, so git never asks for a name or a password:

1. Under **Setup → Integrations**, the code host or the tickets show **Your own git on this
   machine** above the workspace choice, with the lines to add. Copy them.
2. Open your global git configuration with `git config --global --edit` and paste the lines
   **at the end**:

   ```ini
   [credential "http://127.0.0.1:7878"]
   	helper =
   	helper = "!'/Users/you/.guildbotics/bin/guildbotics' git-credential"
   ```

   The empty `helper =` stops git from asking the helpers configured before it (the OS
   keychain, Git Credential Manager) for Branvil. A helper configured after these lines would
   be asked to store the token, so keep them last.
3. `git fetch origin` and `git push origin <branch>` now work without a prompt.

On its first request, the helper asks the running GuildBotics server to issue the owner a token
for the requesting machine. A valid cached token is checked directly at its own origin and reused without asking
the GuildBotics API; issuance requires the server to be running. The token is labelled `git:<device name> (<last 8 characters of the device ID>)`
and kept in the machine's OS keychain only. It is never written to a URL, a git configuration,
your shell history, or a workspace file. You can revoke it under Branvil's **Settings → Members →
owner → Tokens**. Once Branvil no longer accepts the token, the next request issues a new one in
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

The ticket workflow and pull request patrol read the Branvil board and conversations:

- A ticket has at most one assignee: the agent that works on it. Assigning another
  member replaces the assignee, so two members never take up the same ticket.
- Every ticket is in a lane of its workspace's board: a ticket a member creates goes to
  **New**, and `member ticket create --no-add-to-board` is refused.
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
  After a member has reviewed 3 heads, automatic re-review not driven by a request
  stops when a head that member has never reviewed is pushed: the host ticket selector makes the
  pull request a draft during candidate selection, records it in Activity, and
  announces it on the PR and in the reviewer’s `notify_work_stops: true` chat channels. Statements and thread replies on an already reviewed head still
  start a run after the limit, including heads restored by force push or reviewed
  before the count restarted. Explicit review requests remain outside the limit.
  Mark it ready for review to go on; every reviewer's count starts over.
- A member does not wait for CI inside its run: when only unfinished checks are left, it
  ends the run with a waiting notice on the pull request, and the patrol resumes it once
  the checks finish or new commits land.
- Draft pull requests are left alone. Only the owner makes a draft ready for review.
- A run that fails or is rate-limited leaves a status notice on the ticket or the pull
  request; Branvil shows it as a notice, and it holds the item until someone acts on it.

## Repositories with an upstream on GitHub

A Branvil repository may follow a GitHub repository (its *upstream*; see the Branvil
README). GitHub then stays the place of record, and Branvil is where the members try
things:

- Members work in Branvil exactly as above. Nothing reaches GitHub while they do.
- When a pull request is done, the owner publishes it from Branvil's GUI instead of
  merging: Branvil pushes the branch and opens a pull request on GitHub with the final
  title and body as the author's GitHub account. Configure each account in
  Branvil's member settings. [Upstream setup](../crates/branvil/README.md#upstream-github)
- GitHub's checks of the published head appear among the pull request's checks
  (`github/<name>`) with their logs, so a failure on GitHub (Windows, for example) keeps
  the pull request from being ready, and the member fixes it in Branvil; later pushes
  reach GitHub on their own.
- People approve and merge on GitHub. Branvil then follows GitHub's default branch and
  marks its pull request merged, so the patrol moves the ticket to **Done** as usual.
- A ticket can name the GitHub issue it stands for; the published pull request then
  closes that issue.
- GitHub's Dependabot alerts of the upstream are read through Branvil
  (`member repo-info read --resource dependency_alerts`,
  `repository/security_alerts`): Branvil reads them with the account that reads GitHub
  for it, which then needs **Dependabot alerts: Read-only** and access to the
  repository (a GitHub App's added permission approved on its installation). A
  repository without an upstream has no alerts and refuses the read as unsupported.

### Initial ticket state

Ticket creation sends interactive mode, assignee, lane, parent and blockers in
one request. The server commits them together; patrol never observes a partially
configured ticket. Interactive tickets and their descendants are excluded from
patrol. Without an explicit lane, a ticket with a parent or blockers starts in
`ready`; other tickets start in `new`.
