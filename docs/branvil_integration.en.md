# Branvil Integration Guide

Branvil is a code host and board you run yourself: Git repositories over Smart HTTP in
workspaces, pull requests with reviews and CI, and each repository's tickets on its
workspace's board. GuildBotics uses it through the `branvil` provider as the code host
(`services.code_hosting_service`) and the tickets (`services.ticket_service`), in place of
GitHub. The chat stays whatever the
project chooses (Slack, for example).

This guide covers what GuildBotics needs of Branvil. How to install and run Branvil, and
the exact `branvil` commands, are in the Branvil README.

Ticket mention selection uses the `mentions` lists Branvil returns on bodies
and comments. Branvil resolves names against its members when a body is saved.
Install the matching Branvil (storage schema 22) before this provider.
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
│ branvil serve                │            │ GuildBotics (forwards)       │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics                  │            │                              │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil runs once, on the machine that hosts the GuildBotics Hub, and listens on
  loopback only.
- Every machine reaches it at the same base URL, `http://127.0.0.1:7878` by default. On
  the Hub machine that is Branvil itself. On any other machine, GuildBotics (Desktop,
  or the service of `guildbotics start`) forwards that local port to the Hub over the
  SSH connection the Hub sync already uses, for as long as it runs, so nothing else
  needs to run there. While GuildBotics is not running, `branvil tunnel --via <hub>`
  forwards it instead (for `git` or Branvil's GUI). Whichever of them binds the port
  first forwards it; the other waits, and GuildBotics takes the port over within
  10 seconds once it is free. Only a loopback base URL (`127.0.0.1` or `localhost`)
  is forwarded.
- The same base URL everywhere is what keeps recorded URLs valid: a ticket is
  `{base}/{repository}/-/tickets/{n}` and a pull request `{base}/{repository}/-/pulls/{n}`
  on every machine, and GuildBotics' connection to Branvil names one base URL. A repository
  numbers its tickets (`#1`) apart from its pull requests (`!1`).

When nothing forwards the port, every Branvil operation fails with "Branvil is not
reachable at …", and **Setup → Verification** reports *Branvil is not reachable*.

A member command invoked through a running command's broker retries failed connections
to Branvil within its remaining execution time. For example, if 50 seconds of its
300-second budget have elapsed, a Branvil operation or `member git` before fetching or
pushing can wait for the remaining 250 seconds. Requests share that deadline; starting
another request does not renew the budget. An attempt in progress is also bounded by
the deadline. Exhausting it raises `Unreachable`; a known unconnected request also
has the `ServiceUnavailable` type described below.

Without a caller deadline (a scheduler patrol or a standalone interactive member CLI),
each request gets a 300-second operation budget. Five minutes limits how long one
service call may occupy a worker or interactive CLI; it is not an estimate of how
long Branvil needs to restart. Host calls that select tickets, move their lanes or
post workflow status comments also use this budget. The ordinary HTTP timeout still
applies to each attempt. Once a connection wait has run out, no client in the same
process retries that base URL until any client observes a response. Recreating a
context, or another member's patrol, does not renew the wait. Member, admin and health
responses all clear this state; a failed admin or health probe does not set it.
A request Branvil may already have
received is never sent again, so a write does not land twice. The setup screens and the check that
Branvil answers do not wait: they report whether Branvil answers now. Through the SSH
forward of a machine other than the Hub, a Branvil that refuses connections looks like a
connection that was accepted and then dropped, so there it is not waited for.

When no connection could be made, the provider raises the port's `ServiceUnavailable`
type. Autonomous patrols and scheduled commands defer that work: they leave no failed
workflow comment, record a started run as `deferred` (shown as information in Activity),
and preserve the worker's consecutive-error count. Other work, including chat, keeps
running. Later patrols probe Branvil without another connection wait and resume work
when it answers. A manual command still returns its error. Authentication refusals,
timeouts without proof of nonconnection and failures after a request may have arrived remain failures;
they are never silently retried as unavailable work.
An unavailable scheduled occurrence stays due and is retried on a later scheduler cycle.

## Prepare Branvil

Following the Branvil README:

1. Run Branvil on the Hub machine. Other machines need nothing more while GuildBotics
   runs there. `branvil init` creates Branvil's owner, the one person who uses it: the
   GuildBotics human member. The owner may do everything, merges, and uses Branvil's GUI.
2. Create the repositories the members work in (`acme/demo`, with the labels its tickets
   may carry) in Branvil's GUI. A workspace (`acme`) holds repositories and has one board;
   a repository holds its tickets; a merge closes only tickets of the pull request's
   repository. GuildBotics can create the workspace itself (below).

Members and their tokens are not created in Branvil: GuildBotics does that.

Several GuildBotics workspaces can use one Branvil. The connection to Branvil and each
member's agent and token belong with the members' identities, which every workspace
shares, so connecting once serves every workspace, and each workspace chooses the Branvil
workspace its members work in.

## Connect GuildBotics to Branvil

On the Hub machine, where Branvil runs, under **Setup → Integrations** in the desktop app:

1. Choose **Branvil** for the code host and for the tickets. Both use the one Branvil
   GuildBotics is connected to.
2. Check the **Base URL** next to **Connect to Branvil** (`http://127.0.0.1:7878` unless
   changed), then press **Connect to Branvil**. The browser opens Branvil's consent screen: *Allow
   GuildBotics to manage Branvil?* Choose **Allow**. (When the browser has no Branvil
   session yet, the page says to run `branvil open` on this machine; it comes back to the
   consent screen once you have entered.) The screen then shows *Connected to Branvil as
   its owner*. The connection, with its base URL, is the same in every workspace:
   another workspace that chooses Branvil is connected already.
3. Choose the **Workspace** the members work in, or create one there. Members write only
   to its repositories and take tickets from its board; reads are not limited.

Branvil issues GuildBotics an owner token labelled `app:GuildBotics`. GuildBotics keeps
it in the OS keychain as `BRANVIL_ADMIN_TOKEN`, in the identities' store that every
workspace shares, and the base URL beside it in
`~/.guildbotics/identity/.guildbotics/config/connections/branvil.yml`. It uses the token only in
its own process on the machine (the setup screens and the checks): no member, command, or
isolated environment receives it. To revoke it, delete the token under Branvil's
**Settings → Members → owner → Tokens**, then connect again.

On the other machines there is nothing to connect: the connection's file reaches them with
the members' identities, and the Secrets are sent from the Hub machine and fetched under
**Members' credentials on this machine** (the admin token and the members' tokens alike).
No machine issues them again.

**Open Branvil**, next to the connection, opens Branvil's GUI in the system browser from
any machine: GuildBotics asks Branvil for a single-use entry link with the admin token.
The **Branvil** item below **Setup** in the sidebar opens it the same way, with an
external-link icon. It appears when the saved code host or ticket service uses Branvil;
saving the integration settings updates its visibility immediately. If opening fails,
the reason appears below the item.

## Set up the members

Under **Setup → Members → Branvil**:

- An AI member is a Branvil agent: **Create a new agent** (named after the member's ID)
  or **Choose an existing agent**. Saving the member creates the agent unless one was
  chosen, issues its token (labelled `guildbotics`), and keeps the token in the OS
  keychain as `{PERSON_ID}_BRANVIL_TOKEN` (its ID as `{PERSON_ID}_BRANVIL_TOKEN_ID`). The
  token it replaces is revoked once the save is done; when the save is refused, the token
  issued for it is revoked instead and the one held before keeps working. No token is ever
  shown or typed. The token is also the member's git password: git authenticates with
  the agent's name and the token. The agent and its token are the member's in every
  workspace: a save in any workspace keeps the token while Branvil accepts it as the
  agent's; **Issue a new token when saved** replaces it.
- The human member is Branvil's owner: its Branvil name is filled in with the owner's
  when the member is saved.
- The member's **GitHub** tab is shown too. When an AI member has a GitHub account there
  (a machine user's token, or a GitHub App), every save hands it to Branvil, which
  publishes the member's pull requests as that account (below).
- The member's avatar is Branvil's picture of its agent (or of the owner, for the human
  member): GuildBotics hands it over on every save and whenever the avatar is uploaded or
  imported. It is set in GuildBotics only.
- Removing a member from a workspace (**Remove from this workspace**) revokes nothing: the
  other workspaces keep using its token. Deleting an AI member (**Delete**) revokes the
  `guildbotics` tokens of its agent. The agent stays in Branvil, as its history names it.

Saving an AI member and deleting one reach Branvil, so they need Branvil to be reachable
and GuildBotics to be connected on that machine; otherwise nothing is saved or deleted,
and the screen says why.

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

and in each member's `identity.yml`, which a save of the member writes:

```yaml
account_info:
  branvil_username: aiko
```

Press **Validate settings** under **Setup → Verification**: it checks that Branvil
answers, that this machine holds GuildBotics' connection and Branvil takes it for its
owner (the human member), that each AI member's token is accepted as that member's own,
and that the workspace can be read.

## Use Branvil from your own checkouts

You, the human member, are Branvil's owner, and you can make Branvil the `origin` of your own
checkouts on any machine where GuildBotics runs. Branvil's git asks for a member's name and
token. GuildBotics hands git yours, so git never asks for a name or a password:

1. Under **Setup → Integrations → Branvil**, while connected, **Your own git on this machine**
   shows the lines to add. Copy them.
2. Open your global git configuration with `git config --global --edit` and paste the lines
   **at the end**:

   ```ini
   [credential "http://127.0.0.1:7878"]
   	helper =
   	helper = "!'/Users/you/.guildbotics/bin/guildbotics' git-credential"
   ```

   The empty `helper =` stops git from asking the helpers configured before it (the OS
   keychain, Git Credential Manager) for Branvil. A helper configured after these lines would
   be asked to store the token, so keep them last. On Windows the helper is
   `!guildbotics git-credential`, since the installer puts the CLI on `PATH`.
3. `git fetch origin` and `git push origin <branch>` now work without a prompt.

On its first request on a machine, GuildBotics issues the owner a token for that machine with
its connection. The token is labelled `git:<device name> (<last 8 characters of the device ID>)`
and kept in that machine's OS keychain only. It is never written to a URL, a git configuration, your shell
history, or anything the Hub shares. Each machine has its own token, and you can revoke one
machine's token under Branvil's **Settings → Members → owner → Tokens**. Once Branvil no
longer accepts a machine's token, the next request issues that machine a new one in its place
and revokes the machine's other tokens (such as one labelled with its name before a rename).

The token is a full owner token, the same trust as the connection the machine already holds.
Revoking a machine's token does not lock that machine out, because GuildBotics there issues
another. To cut a machine off, remove it from the Hub, revoke the `app:GuildBotics` token and
connect again (the other machines fetch the new connection from the Hub), then revoke that
machine's `git:` token.

When the helper cannot hand a token (Branvil does not answer, the connection's token is not on
that machine, or the keychain cannot be used), git stops and prints why, without asking for a
password. When GuildBotics on that machine is not connected to the Branvil of the remote at all,
the helper says so, and git falls back to asking for a name and a password: connect, or fetch the
connection from the Hub, instead of answering. The members' own git never uses this helper. GuildBotics hands member git the agent's
token and drops every configured helper.

## How members work with it

The ticket workflow and the pull request patrol behave as they do with GitHub:

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
  Automatic re-review stops after 3 reviewed heads, only when the current head has
  never been reviewed by that member: the patrol makes the pull request a draft
  and says so on it. Statements and thread replies on an already reviewed head still
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
  title and body, **as the author's GitHub account**. Give each AI member its bot
  account on the member's **GitHub** tab in GuildBotics; GuildBotics hands it to Branvil.
  One of these bot accounts also reads GitHub for Branvil, so the human member's own
  GitHub token is not needed.
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
