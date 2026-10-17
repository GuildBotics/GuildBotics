# Branvil Integration Guide

Branvil is a code host and board you run yourself: Git repositories over Smart HTTP in
workspaces, pull requests with reviews and CI, and each repository's tickets on its
workspace's board. GuildBotics uses it through the `branvil` provider as the code host
(`services.code_hosting_service`) and the tickets (`services.ticket_service`), in place of
GitHub. The chat stays whatever the
project chooses (Slack, for example).

This guide covers what GuildBotics needs of Branvil. How to install and run Branvil, and
the exact `branvil` commands, are in the Branvil README.

## How it fits together

```text
Hub machine                                 another machine
┌──────────────────────────────┐            ┌──────────────────────────────┐
│ branvil serve                │            │ branvil tunnel --via <hub>   │
│   http://127.0.0.1:7878      │◀── SSH ────│   http://127.0.0.1:7878      │
│ GuildBotics                  │            │ GuildBotics                  │
└──────────────────────────────┘            └──────────────────────────────┘
```

- Branvil runs once, on the machine that hosts the GuildBotics Hub, and listens on
  loopback only.
- Every machine reaches it at the same base URL, `http://127.0.0.1:7878` by default. On
  the Hub machine that is Branvil itself; on any other machine, Branvil's tunnel
  forwards the same local port to the Hub over the SSH connection the Hub sync already
  uses. GuildBotics has no tunnel of its own.
- The same base URL everywhere is what keeps recorded URLs valid: a ticket is
  `{base}/{repository}/-/tickets/{n}` and a pull request `{base}/{repository}/-/pulls/{n}`
  on every machine, and the shared project settings name one `base_url`. A repository
  numbers its tickets (`#1`) apart from its pull requests (`!1`).

When the tunnel is down, every Branvil operation fails with "Branvil is not reachable at
…", and **Setup → Verification** reports *Branvil is not reachable*.

## Prepare Branvil

Following the Branvil README:

1. Run Branvil on the Hub machine, and the tunnel on every other machine that runs
   GuildBotics. `branvil init` creates Branvil's owner, the one person who uses it: the
   GuildBotics human member. The owner may do everything, merges, and uses Branvil's GUI.
2. Create the repositories the members work in (`acme/demo`, with the labels its tickets
   may carry) in Branvil's GUI. A workspace (`acme`) holds repositories and has one board;
   a repository holds its tickets; a merge closes only tickets of the pull request's
   repository. GuildBotics can create the workspace itself (below).

Members and their tokens are not created in Branvil: GuildBotics does that.

## Connect GuildBotics to Branvil

On the Hub machine, where Branvil runs, under **Setup → Integrations** in the desktop app:

1. Choose **Branvil** for the code host and for the tickets. Both use one Branvil, at one
   **Base URL** (leave it empty for `http://127.0.0.1:7878`).
2. Press **Connect to Branvil**. The browser opens Branvil's consent screen: *Allow
   GuildBotics to manage Branvil?* Choose **Allow**. (When the browser has no Branvil
   session yet, the page says to run `branvil open` on this machine; it comes back to the
   consent screen once you have entered.) The screen then shows *Connected to Branvil as
   its owner*.
3. Choose the **Workspace** the members work in, or create one there. Members write only
   to its repositories and take tickets from its board; reads are not limited.

Branvil issues GuildBotics an owner token labelled `app:GuildBotics`. GuildBotics keeps
it in the OS keychain as the workspace Secret `BRANVIL_ADMIN_TOKEN` and uses it only in
its own process on the machine (the setup screens and the checks): no member, command, or
isolated environment receives it. To revoke it, delete the token under Branvil's
**Settings → Members → owner → Tokens**, then connect again.

On the other machines there is nothing to connect: send the Secrets from the Hub machine
and fetch them under **Credentials on this machine** (the admin token and the members'
tokens alike). No machine issues them again.

**Open Branvil**, next to the connection, opens Branvil's GUI in the system browser from
any machine: GuildBotics asks Branvil for a single-use entry link with the admin token.

## Set up the members

Under **Setup → Members → Branvil**:

- An AI member is a Branvil agent: **Create a new agent** (named after the member's ID)
  or **Choose an existing agent**. Saving the member creates the agent unless one was
  chosen, issues its token (labelled `guildbotics`, replacing the one issued before), and
  keeps the token in the OS keychain as `{PERSON_ID}_BRANVIL_TOKEN`. No token is ever
  shown or typed. The token is also the member's git password: git authenticates with
  the agent's name and the token. Later saves keep the token while Branvil accepts it as
  the agent's; **Issue a new token when saved** replaces it.
- The human member is Branvil's owner: its Branvil name is filled in with the owner's
  when the member is saved.
- The member's **GitHub** tab is shown too. When an AI member has a GitHub account there
  (a machine user's token, or a GitHub App), every save hands it to Branvil, which
  publishes the member's pull requests as that account (below).
- Deleting an AI member revokes the `guildbotics` tokens of its agent. The agent stays in
  Branvil, as its history names it.

Saving an AI member and deleting one reach Branvil, so they need Branvil to be reachable
and GuildBotics to be connected on that machine; otherwise nothing is saved or deleted,
and the screen says why.

Without the GUI, the same settings are in `team/project.yml`:

```yaml
services:
  code_hosting_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    workspace: acme
  ticket_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    workspace: acme
```

and in each member's `person.yml`, which a save of the member writes:

```yaml
account_info:
  branvil_username: aiko
```

Press **Validate settings** under **Setup → Verification**: it checks that Branvil
answers, that this machine holds GuildBotics' connection and Branvil takes it for its
owner (the human member), that each AI member's token is accepted as that member's own,
and that the workspace can be read.

## How members work with it

The ticket workflow and the pull request patrol behave as they do with GitHub:

- A ticket has at most one assignee: the agent that works on it. Assigning another
  member replaces the assignee, so two members never take up the same ticket.
- A ticket assigned to the member in the **Ready** lane of the workspace's board is
  taken up and moved to **In progress**; one in **In progress** is taken up again when
  someone else has spoken since the member last did. Once a pull request refers to the
  ticket, the patrol follows the pull request instead, and its merge moves the ticket to
  **Done**.
- A member's pull request is taken up when someone says something the member has not
  answered; a pull request the member reviews, when a review is requested, when new
  commits land after its last review, or when there is a statement it has not answered.
  Automatic re-review stops after 3 rounds: the patrol makes the pull request a draft
  and says so on it. Mark it ready for review to go on.
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
