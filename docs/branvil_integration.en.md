# Branvil Integration Guide

Branvil is a code host and board you run yourself: Git repositories over Smart HTTP,
pull requests with reviews and CI, and trackers of tickets on a board. GuildBotics uses
it through the `branvil` provider as the code host (`services.code_hosting_service`) and
the tickets (`services.ticket_service`), in place of GitHub. The chat stays whatever the
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
  `{base}/{tracker}/-/tickets/{KEY}` and a pull request `{base}/{repository}/-/pulls/{n}`
  on every machine, and the shared project settings name one `base_url`.

When the tunnel is down, every Branvil operation fails with "Branvil is not reachable at
…", and **Setup → Verification** reports *Branvil is not reachable*.

## Prepare Branvil

Following the Branvil README:

1. Run Branvil on the Hub machine, and the tunnel on every other machine that runs
   GuildBotics.
2. Create the namespace the members write in (for example `acme`), with its
   repositories (`acme/demo`), a tracker for the tickets (`acme/demo-tickets`, whose
   repository is `acme/demo` so that a merge can close its tickets), and a board
   (`acme/board`).
3. Create a Branvil member for each GuildBotics member: an agent member for each AI
   member, and the humans who assign tickets and review. Grant the members `write` on
   the namespace.
4. Issue a token for each agent member. Branvil shows it once.

## Configure GuildBotics

Under **Setup → Integrations** in the desktop app, choose **Branvil** for the code host
and for the tickets, and enter:

- **Base URL**: where Branvil is reached (leave it empty for `http://127.0.0.1:7878`)
- **Namespace**: the namespace the members write in (`acme`). Members write only to
  repositories and trackers directly in it; reads are not limited
- **Board** (tickets only): the board the tickets are taken from (`acme/board`).
  Without a board, no ticket is selected as a member's work

Then, under **Setup → Members → Branvil**, give each member its **Branvil username**
and, for an AI member, its **Branvil token**. The token is stored in the OS keychain as
`{PERSON_ID}_BRANVIL_TOKEN` and is also the member's git password: git authenticates
with the username and the token.

Without the GUI, the same settings are in `team/project.yml`:

```yaml
services:
  code_hosting_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    owner: acme
  ticket_service:
    name: branvil
    base_url: http://127.0.0.1:7878
    owner: acme
    board: acme/board
```

and in each member's `person.yml`:

```yaml
account_info:
  branvil_username: aiko
```

Press **Validate settings** under **Setup → Verification**: it checks that Branvil
answers, that each AI member's token is accepted as that member's own, and that the
board can be read.

## How members work with it

The ticket workflow and the pull request patrol behave as they do with GitHub:

- A ticket assigned to the member in the board's **Ready** lane is taken up and moved to
  **In progress**; one in **In progress** is taken up again when someone else has spoken
  since the member last did. Once a pull request refers to the ticket, the patrol
  follows the pull request instead, and its merge moves the ticket to **Done**.
- A member's pull request is taken up when someone says something the member has not
  answered; a pull request the member reviews, when a review is requested, when new
  commits land after its last review, or when there is a statement it has not answered.
  Automatic re-review stops after 3 rounds: the patrol makes the pull request a draft
  and says so on it. Mark it ready for review to go on.
- Draft pull requests are left alone. Only a human member makes a draft ready for review.
- A run that fails or is rate-limited leaves a status notice on the ticket or the pull
  request; Branvil shows it as a notice, and it holds the item until someone acts on it.
