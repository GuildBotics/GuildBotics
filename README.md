<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

GuildBotics lets you run AI CLI tools such as Claude Code or Codex as team members who keep working with your development team. Each member has its own name, Branvil / Slack account, roles, and memory. The actual investigation, coding, and judgment are delegated to the AI CLI tool, while every external action the member takes (commits, PR creation, comments, Slack posts, memory writes) goes through a single dedicated CLI (`guildbotics member`), where it is executed and recorded.

Branvil is the code host and board bundled with GuildBotics. It keeps repositories, pull requests, CI, and tickets on the Hub and gives each AI member its own account. You can use it in place of GitHub, or have it follow a GitHub repository and send only finished pull requests to GitHub (→ [Branvil integration guide](docs/branvil_integration.en.md)).

There are two ways to work with the same member:

- **Work together** — invoke the member in your Claude Code or Codex session and pair-program in the repository you currently have open (→ [Work Together with a Member](#work-together-with-a-member))
- **Delegate** — ask through a Branvil ticket or a Slack message, and the member investigates, implements, opens the pull request, and replies on its own (→ [Delegate Branvil Tickets](#delegate-branvil-tickets) / [Ask for Work in Slack](#ask-for-work-in-slack))

Both ways drive the same member — same identity, same memory. What you teach while working together is kept as memory and carries over to the work you delegate.

You configure, run, and monitor GuildBotics in its screens, which the GuildBotics server on your machine serves to a browser. Workspace configuration is stored in text files. To move it to another Hub, see [Run on a Server](#run-on-a-server).

---

## Important Notice (Disclaimer)

- This software is in alpha version. There is a very high possibility of breaking incompatible changes in the future, and malfunctions are expected to occur frequently, so use in production environments is not recommended.
- The author and distributor assume no responsibility for malfunctions of this software or damages caused thereby. In particular, due to malfunctions or runaway of AI agents, there is a possibility of fatal destruction to the system in use or external services, data loss, and leakage of confidential data. Use at your own risk and verify in an isolated test environment.

---

- [What You Can Do](#what-you-can-do)
- [Getting Started](#getting-started)
- [Work Together with a Member](#work-together-with-a-member)
- [Delegate Branvil Tickets](#delegate-branvil-tickets)
- [Ask for Work in Slack](#ask-for-work-in-slack)
- [Automate Recurring Work](#automate-recurring-work)
- [Create Your Own Commands](#create-your-own-commands)
- [Operations Reference](#operations-reference)
- [Troubleshooting](#troubleshooting)

---

## What You Can Do

- **Multiple members**: Define multiple AI members with distinct roles, personalities, and memory (the identifier in configuration files is `person`)

- **Slack integration**: Members watch configured channels and handle the requests they receive there as themselves
- **Branvil integration**: Use [Branvil](docs/branvil_integration.en.md), a code host and board you run yourself on the Hub machine, in place of GitHub for repositories, pull requests, CI, and tickets
- **Member memory**: Personal and team memory that members recall and maintain across sessions
- **Interactive member sessions**: The guildbotics skill lets an AI CLI tool work as a member in your current repository
- **Scheduled execution**: Per-member patrol commands and cron-based scheduled commands
- **Custom commands**: Define your own work as a Markdown prompt / Python / Shell / YAML command and reuse it per member or role
- **Swappable LLM / AI CLI tool**: Swap LLM providers or delegate to an AI CLI tool (Codex, Claude Code, Grok Build, Antigravity, and other AI tools launched from a local CLI)
- **AI assistants in the screens**: Ask questions and apply proposed source changes in the command editor, and investigate execution failures in the diagnostics screen
- **Internationalization**: English / Japanese

## Getting Started

### What You Need

- An Apple Silicon Mac for the Hub, Git, the Rust toolchain pinned in `rust-toolchain.toml`, and Node.js 24 with npm to build the screens. CI runs on Linux.
- To use a browser on another machine, enable Remote Login on the Hub and install an OpenSSH client on that machine. Windows can be a client.
- For model-backed commands such as translation, an API key for Google Gemini, OpenAI, or Anthropic. To use an AI CLI member, log in on the Hub after setup with `guildbotics environment login <tool>`. The environment provisions Codex, Claude Code, Grok Build, GitHub Copilot CLI, and Antigravity.

### Installation

Build the screens and the executable from the Hub checkout. To try the setup from a terminal:

```sh
git clone https://github.com/GuildBotics/GuildBotics.git
cd GuildBotics
npm ci && npm run build --workspace desktop --workspace crates/branvil/web
cargo build --locked -p guildbotics
target/debug/guildbotics install
# To include Branvil, run these two lines before starting the server.
# export BRANVIL_DATA_DIR="$HOME/branvil-data"
# "$HOME/.guildbotics/bin/guildbotics" branvil init --data-dir "$BRANVIL_DATA_DIR"

"$HOME/.guildbotics/bin/guildbotics" server start
```

In another terminal, run `"$HOME/.guildbotics/bin/guildbotics" open` to open the setup screen. To use an existing workspace, select it with `guildbotics workspace use <path>` before starting the server.

`install` places the executable in `~/.guildbotics/bin/guildbotics` and copies the bundled skill into the user directories of the AI CLI tools it finds. It preserves edited skills and user-owned commands. Add `~/.local/bin` to `PATH` to use the link there.

For a first-time Branvil setup, run the two optional lines above. If you already have Branvil data, set `BRANVIL_DATA_DIR` to its absolute path before starting. In the terminal where you open Branvil, set the same `BRANVIL_DATA_DIR` and run `"$HOME/.guildbotics/bin/guildbotics" open --branvil`. For login startup, updates, and recovery, see the [macOS Hub guide](docs/hub-server.md).

### Initial Setup

Open the screens with `guildbotics open` on the Hub. The browser opens on **Project** setup, where you configure:

- The agent default language (English / Japanese), used for command and role definitions and for LLM output instructions
- The workspace folder. Keep it outside the repository the agents work on: for example, use `~/repos/myproject` for the repository and `~/teams/myproject` for the workspace. It also cannot be inside the exchange folder `~/Documents/GuildBotics`
- The project description

**Integrating with Branvil or Slack requires preparation on the service side before you start configuring.** Every integration is optional and can be enabled later under **Setup → Integrations**. If you just want to see GuildBotics run, leave them unused and move on.

- **Slack**: for members to take requests in Slack, you need a Socket Mode Slack App and its bot / app tokens. See [Ask for Work in Slack](#ask-for-work-in-slack) for what to prepare
- **Branvil**: a running Branvil, reachable at the same base URL on every machine. GuildBotics connects to it once on the owner's consent and sets up each member's Branvil agent and token itself. Your own checkouts can use it as their `origin` too. See the [Branvil Integration Guide](docs/branvil_integration.en.md)

In GuildBotics, the folder that holds a project's settings and state is called the **workspace**. Plain text configuration files are written there:

- `.guildbotics/local/debug.env`: device-local, non-secret debug settings (log level)
- `.guildbotics/config/secrets.yml`: key names in the encrypted store. The index contains no values.
- `.guildbotics/config/team/project.yml`: project definition
- `.guildbotics/config/intelligences/`: LLM and AI CLI tool settings

Each member's identity (name, character, avatar, and service accounts) is the same in every workspace, so it is written outside the workspace, to `~/.guildbotics/identity/`, together with the members' credentials. The workspace keeps what each member does there (roles, patrol, chat channels, AI settings) (→ [Configuration Files](#configuration-files)).

API keys and account tokens are kept in an encrypted local store, and only their key names are recorded in the files above (→ [Storing API Keys and Tokens](#storing-api-keys-and-tokens)).

After project setup, configure the following on the screens:

- **LLM / AI CLI tools**: default LLM, AI CLI tool selection, and LLM API keys
- **Agent execution environment**: build the environment on this machine with **Build**, and log in to the selected tool with the command it shows. Both are per machine
- **Integrations**: choose and configure the services for code hosting, tickets, and chat. Each is optional.
- **Members**: add and configure team members (service accounts are configured on the member’s service tab). The name, character, avatar, and service accounts are common to every workspace; roles, patrol, chat, and AI settings are for this workspace only. A member's ID cannot be changed once saved. **Add an existing member** adds a member another workspace already has. **Remove from this workspace** takes the member out of this workspace only (its identity, credentials, and service tokens stay). **Delete** removes the member from this workspace, deletes its identity and credentials, and revokes the tokens GuildBotics issued it on services such as Branvil; another workspace that still has the member leaves it out until it is removed there too
- **Verification**: press **Validate settings** to run a read-only check across LLM, AI CLI tool, the integrated services, and Git. It does not update the integrated services' data

### Quick Start

Confirm that setup is complete by running a custom command. Open the **Edit Command** screen.

The initial setup placed sample commands in `.guildbotics/config/commands/` under your workspace. This walkthrough uses the `translate` sample.

1. Pick `translate` from the command list.

2. Type `Hello` into **Input text** in the run panel and press **Save and run**.

You are set if the translated text appears under **Output**. The default LLM you configured was called, and your custom command works.

### What Next

- Pair-program with a member in the repository you have open → [Work Together with a Member](#work-together-with-a-member)
- Delegate everything from ticket to pull request → [Delegate Branvil Tickets](#delegate-branvil-tickets)
- Have a member handle requests in Slack → [Ask for Work in Slack](#ask-for-work-in-slack)
- Automate recurring work or build your own commands → [Automate Recurring Work](#automate-recurring-work) / [Create Your Own Commands](#create-your-own-commands)

## Work Together with a Member

`guildbotics install` installs the **guildbotics skill** into the user skill directory of each detected AI CLI tool, such as Claude Code or Codex (→ [Installation](#installation)). That skill is what lets you work together with a member in the repository you currently have open.

The skill lives in the user skill directory each tool reads (`~/.claude/skills` for Claude Code, `~/.agents/skills` for Codex, and so on), so the same skill is used whether you start the tool from its CLI or from its app. The install prints where it put the skill for each tool, and which copies it kept because you had edited them.

There are four prerequisites:

- The AI CLI tool is installed on your machine and you are logged in to it there. This is the one place a host install is needed: it is you working interactively, not a member's turn, which runs inside the isolated agent environment
- The guildbotics skill and the managed CLI are installed (→ [Installation](#installation))
- A member is configured
- The GuildBotics server is running, as the service or with `guildbotics server start`. All member commands, including `--help`, use that server; the CLI and the server must come from the same checkout

Start your AI CLI tool in the repository you want to work in and ask for work, **naming the skill and the member**. That phrase is what invokes the skill.

```text
Use the guildbotics skill and commit and push this change as the member alice
```

Member commands use the workspace selected by the server. `member --workspace <path>` selects a local workspace ID; it does not switch the server. Finish active work and switch the workspace under **Setup → Project** first, or stop the server with `guildbotics server stop`, run `guildbotics workspace use <path>`, and start it again. File content, downloads and commits in your current repository are handled by the CLI; provider credentials stay in the server.

The AI CLI tool loads the member's profile (roles, judgment criteria, speaking style) and memory, and responds as that member from then on. External actions such as commits, pushes, PR creation, and Slack posts are performed under the member's own identity.

If you say "remember this" during the session, it is saved as the member's memory. That memory is also used when you delegate work through tickets or Slack.

You can also ask another member for a one-off task during the session:

```text
Ask bob to review the current changes, including uncommitted edits.
```

Use a repository that contains no workspace state as the working directory for `ask`. If the working repository is also your workspace, move the workspace elsewhere before delegating work.

The active member runs the built-in `ask` command and relays bob's result. Bob sees the same working tree; if you ask him to fix something, his edits appear there too. Select the workspace in GuildBotics first, and prepare bob's isolated agent environment and AI CLI login on this machine under **Setup → Agent execution environment**. The host CLI login used for your interactive session is separate. Reviews can take several minutes; an unavailable environment or login is reported back in the session.

Host `guildbotics run` commands execute on the server when it runs with the same workspace; when no server runs, or it has a different workspace selected, they run locally. Once the server receives a command, an error or lost connection ends the request without repeating it locally. If the member is busy, its refusal reason is returned. A command run on the server returns output only when it finishes and does not forward progress logs or the calling shell's environment variables.

On macOS, grant Documents folder access once to the app that launches GuildBotics under **System Settings → Privacy & Security → Files & Folders** (when you run `guildbotics server start` in a terminal, that terminal app). GuildBotics checks directory access when displaying environment status and before a turn, and reports the same refusal in the CLI and on the screens if access is denied.

## Delegate Branvil Tickets

The default `ticket_driven_workflow` investigates a Branvil ticket, implements the change, and opens a pull request. In **Setup → Integrations**, choose Branvil for code hosting and tickets, and select the workspace. For the connection, member accounts, and board setup, see the [Branvil integration guide](docs/branvil_integration.en.md).

Under **Setup → Agent execution environment**, confirm that the environment is ready and that the AI CLI login is saved. From a terminal, run `guildbotics environment status`.

### Start the Service and Ask for Work

Open the **Service** screen and press **Run**. A worker starts for each active member and runs the work of the selected sources in order. The service runs inside the GuildBotics server, and the server starts alone unless told otherwise. To have the server start the service by itself whenever it starts (at every login when it runs as the service, for example), turn on **Start the services when the server starts** on the same screen (off by default); it applies from the next start of the server. From a terminal, `guildbotics service start` and `guildbotics service stop` do what **Run** and **Stop** do.

The screen has three execution sources, each toggled individually with **Include in service run**; at least one stays included. The choices, the patrol interval, and the failure limit are saved on this machine's server, so every start runs with them: from any screen, from `guildbotics service start`, or by the server as it starts. A change made while the service runs applies from its next start.

- **Patrol commands**: runs each member's patrol commands after the **Patrol interval (minutes)** has passed with no remaining patrol work. The ticket-driven workflow continues through all candidates before the interval starts. When the project uses a ticket service, a new member gets the ticket-driven workflow as its default; otherwise it starts with no patrol commands, so pick them under **Setup → Members → Patrol**
- **Scheduled commands**: runs the scheduled commands defined in member settings at the times you specify
- **Event triggers**: receives events such as Slack messages and routes them to the chat workflow

To use the ticket-driven workflow, start the service with **Patrol commands** included. When **Stop after consecutive failures** (default 3) is reached, that member's worker stops.

You can follow what is happening in the events and logs of the **Global / system** session on the **Diagnostics** screen, and per-member results are collected on the **Activity** screen.

To ask for work, use a Branvil ticket as follows:

1. Create a Branvil ticket in the target repository.
2. Write the instructions for the member in the ticket (this becomes the prompt, so be as specific as possible)
3. Assign the ticket to the member in Branvil.
4. Move the ticket to the ready lane

Once work starts, you interact with the member like this:

- When the member has a question, it posts it as a ticket comment. Answer in a ticket comment and the member picks up your answer on its next check and continues. In-progress tickets with unanswered comments or mentions are picked up even when a linked PR is open or merged; the member reuses an existing open PR
- When the task completes, the member leaves a comment / PR URL / review reply / reaction
- Review the PR created from the ticket on the PR itself. The pull request patrol follows its conversation (→ [Pull Request Patrol](#pull-request-patrol)). Once the ticket's comments and mentions are answered, an open PR keeps the ticket waiting and a merged PR moves it to Done. The member's latest failure or rate-limit status keeps the ticket on hold

To stop, press **Stop**. The service stops accepting new work and stops after in-flight work finishes. If you do not want to wait, press **Force stop** to cancel the in-flight work. The server keeps running and serving the screens; it ends only with `guildbotics server stop` (→ [Run on a Server](#run-on-a-server)).

While a command runs, the machine never goes to sleep from being left idle (idle sleep). To keep it awake while the service waits for work as well, turn on **Keep this machine awake** on the **Service** screen (off by default). Switching it while the service runs takes effect within about a second, and it also applies to a service started from the CLI or by the server as it starts. The setting belongs to this machine only and is saved in the workspace's `.guildbotics/local/service.yml`.

- Closing the lid and putting the machine to sleep by hand still put it to sleep

To start and stop the service from the CLI, see [Run on a Server](#run-on-a-server).

### Pull Request Patrol

Before looking at the ticket lanes, every patrol run checks the open pull requests under the configured Branvil workspace that involve the member. No ticket is needed for this; the PR itself is the unit of work.

In both roles, the member reads everything someone else says, decides what to do only after reading it, and leaves an answer for every statement: a reply saying what it did, a reply with the reason when it does not adopt a point, or a reaction when there is nothing to act on (thanks, LGTM). A statement is an unresolved review thread whose last word is someone else's, a review with a body (approvals included), or a conversation comment. It is answered by a later comment, review, or thread reply from the member, or by the member's reaction on it. Reviews without a body and workflow status notices are not picked up.

- **PRs the member wrote**: statements the member has not answered are picked up. The member fixes the code where the point is valid
- **Waiting for CI**: a member does not wait for CI inside its run. Whatever started the run (a ticket, a chat thread, a scheduled or manual command), when checks that have not finished are all that keeps its PR from being complete, the run ends with a notice on the PR (a ticket's or a chat thread's run shows "Waiting for CI" on the screens), and the patrol picks the same work up again once the checks finish or new commits land on it. The resumed run fixes the failures, or reports the outcome on the PR and where the work came from (the ticket, the thread, or where the command reports). A merged or closed PR is not picked up again
- **PRs the member reviews**: the member starts when a review is requested, when new commits land after its last review, or when there is a statement it has not answered (threads only those it took part in). When statements alone started it and its verdict has not changed, it does not submit a review again, since a review with a body wakes the author's patrol. After a member has reviewed 3 heads, automatic re-review not driven by a request stops when a head that member has never reviewed is pushed: the host ticket selector converts the PR to a draft during candidate selection, records it in Activity, and announces it on the PR and in the reviewer’s chat channels with `notify_work_stops: true` (the author's patrol stops too, as drafts are skipped below). Statements and thread replies on an already reviewed head still start a run after the limit; this includes a head restored by force push and heads reviewed before the count restarted. Explicit review requests remain outside the limit. To go on as it is, mark it **Ready for review**; every reviewer counts its rounds again from the reviews after it is marked ready. To change how it proceeds, give instructions in Slack or an interactive session, and mark it Ready for review once that is settled
- The verdict is submitted as a Branvil review (approve or request changes), which is what consumes a review request and makes the member the PR's reviewer. Request the first review in Branvil, Slack, or an interactive session (→ [Ask for Work in Slack](#ask-for-work-in-slack)); the patrol follows the PR from then on
- **Draft PRs are skipped**, in both roles. Only humans and, at the automatic re-review limit, the host ticket selector convert a PR to a draft, and only humans mark it ready for review again (a member's work cannot change it). To take a PR into your own hands for a while (for example, to rework the approach in an interactive session without another member re-reviewing in the middle), convert it to a draft; the service keeps running for everything else. Mark it **Ready for review** when you are done, and the next patrol picks it up again, including a re-review of the new commits (the automatic re-review rounds, and any member held by a failure, start over from that point)

## Ask for Work in Slack

A member watches the Slack channels you configure and handles the requests it receives there as itself. Ask something like "please handle the review comments on this PR" and the member identifies the target repository, prepares a working branch, actually investigates, edits code, and performs the Branvil operations, then reports the result back in Slack.

How it behaves in Slack (reply with a message / only add a reaction / do nothing / ask a clarifying question / report why it is blocked) is decided by the member (the AI CLI tool) from the message content, its own role and profile, and the per-channel participation policy. It does not force a reply into small talk or into conversations where its role adds nothing.

Two things need to be configured:

First choose Slack under **Setup → Integrations → Chat**; the members then have a Slack tab.

1. **Create a Slack App**: under **Setup → Members → Slack**, choose **Register a new app** and press **Create the app on Slack**. The browser opens with the required scopes, Socket Mode and event subscriptions already configured, so you pick a workspace and create the app. Then press **Reinstall to Workspace** under **OAuth & Permissions** and copy the two tokens (`xoxb-...` / `xapp-...`) — the first bot token Slack issues does not carry the scopes, so the reinstall is required
2. **Register it with the member**: on the same Slack tab, configure the following
   - Paste the **Slack Bot token** (`xoxb-...`) and **Slack App token** (`xapp-...`), then press **Verify app** to confirm the bot name, workspace, granted scopes, and channel membership
   - **Channel**: add the channel names or IDs to watch
   - **When to join**: choose Join actively / Join when needed / Mentions only

If you already have a Slack App, choose **Use a registered app** and start from pasting the tokens.

The tokens are stored with the member's credentials, shared by every workspace. The channels are stored in the workspace's `member.yml` for the member (`message_channels`).

When the member's ticket or pull request work from the patrol or a scheduled run stops to wait for a person — it asked a question, is blocked, or failed and said so on the item — the member posts once to each channel where **Notify when work stops** is enabled in **Setup → Members → Slack** (or the project's chat provider tab), linking the item and its comment. With no channel selected, it posts nothing. Notifications are off by default, including for existing channel settings. Enable the option and save to receive notices, or turn it off and save to stop them.

To receive chat events, start the service with **Event triggers** included on the **Service** screen. Scheduled posts at fixed times (a news digest, for example) go through a separate path: configure `workflows/chat_post_command` as a scheduled command.

For the manual Slack App setup steps (including the list of required scopes), sharing one connection across members, the details of thread participation policies, and scheduled post examples, see the [Slack Integration Guide](docs/slack_integration.en.md).

## Automate Recurring Work

Configure two kinds of automated execution per member under **Setup → Members → Patrol**. Both are run by the service you start on the **Service** screen.

- **Patrol commands**: while the service is running, these run repeatedly in round-robin order (for example, patrolling the task board). The **Patrol interval (minutes)** is the wait after no patrol work remains; a ticket patrol continues through all candidates without waiting for that interval between them. Turn on "Configure patrol commands for this member" and choose the saved commands. Members with it turned off do not run patrol work
- **Scheduled commands**: use **Add schedule** to choose a command and its execution time (for example, periodic reports or cleanup). The time can be an Hourly / Daily / Weekly preset, or a **Detailed schedule** (five-field cron notation)

The patrol interval and the number of consecutive failures that stops a worker are set on the **Service** screen.

Commands the service runs work in the exchange folder (`~/Documents/GuildBotics`), whether the service was started on the **Service** screen, from the CLI, or by the server as it starts: a file a scheduled `.sh` command, or an AI CLI turn, writes to a relative path lands there.

These settings are stored in the workspace's `team/members/<person_id>/member.yml`. To change these settings by hand, edit that file.

```yaml
is_active: true

routine_commands:
  - workflows/ticket_driven_workflow

task_schedules:
  - command: examples/reports/morning_summary
    schedules:
      - "0 9 * * 1-5" # Weekdays at 9:00 AM
```

In addition to standard five-field cron notation, GuildBotics supports its own syntax for randomizing execution times (`?` / `?(min-max)`, useful for avoiding simultaneous execution across members). You can enter this syntax in the **Detailed schedule** field as well.

For cron details, the randomization syntax, how the scheduler works internally, and multi-agent configuration examples, see the [Scheduling Guide](docs/scheduling.en.md).

## Create Your Own Commands

Like the `translate` command you ran in the quick start, you can define your own commands and use them for manual runs, scheduled runs, and scheduled Slack posts. You author and try them out on the **Edit Command** screen.

**Let the AI assistant write it**: choose **Create with AI** under **New command**, describe what you want, and the command name, format, and source are proposed. For the command you are editing, you can ask the **AI assistant** questions or request changes.

Proposed changes are not expanded into the chat history; they appear as read-only editor tabs labeled Current / Create / Update. The Update tab shows added and removed lines as a diff with line numbers, and the Create tab shows the complete source. Nothing is written until you review the content and press **Apply changes**.

**Trying it out**: **Save and run** shows the execution result (output and events) right there. Switch the **Run as** member to run with that member's own settings.

**Command types**:

1. **Markdown commands** (`.md`): executed as LLM prompts. Best for text processing, translation, and summarization
2. **Python scripts** (`.py`): executed with access to project and team member information. Best for complex processing and API integration
3. **Shell scripts** (`.sh`): executed as shell commands
4. **YAML workflows** (`.yml`): compose multiple commands

**Command locations** (in priority order):

1. **Per-member commands**: `.guildbotics/config/team/members/<person_id>/commands/`
2. **Project-shared commands**: `.guildbotics/config/commands/`
3. **Built-in commands**: shipped in the package under `guildbotics/templates/` (fallback; for example `workflows/ticket_driven_workflow`)

The Edit Command screen reads and writes project-shared commands. Per-member commands are placed as files directly. If a file is shadowed by a higher-priority one and would not be the command that actually runs, the screen shows a warning.

The configuration directory defaults to `.guildbotics/config` in the workspace and can be changed with the `GUILDBOTICS_CONFIG_DIR` environment variable.

**A step further**: Markdown commands can declare a template engine and child commands (`commands:`) in front matter. The source below is the `translate` sample you ran in the quick start (`.guildbotics/config/commands/translate.md`): it fetches the OS display language with a child command and switches the translation direction accordingly.

```markdown
---
description: Translate input text between the OS UI language and English, using Japanese when the OS UI language is English.
brain: default
template_engine: jinja2
inputs:
  message: required
commands:
  - name: os_ui_language
    command: functions/get_os_ui_language
---
The input message is structured data.
{% if os_ui_language.language_code == "en" %}
If the text in the `input` field is in Japanese, translate it to English; if it is in English, translate it to Japanese.
{% else %}
If the text in the `input` field is in {{ os_ui_language.language_name }}, translate it to English; if it is in English, translate it to {{ os_ui_language.language_name }}.
{% endif %}
Return only the translated text.
```

`functions/get_os_ui_language` is a built-in command, so no helper file has to be placed in the workspace. The command runs inside the isolated agent environment, which receives the OS UI language of the machine running it as the `LANGUAGE` environment variable (see [Where a command runs](docs/custom_command_guide.en.md#14-where-a-command-runs)).

**How to run a command**:

- **Run** on the **Edit Command** screen (you can supply input text and additional args)
- Patrol / scheduled execution under **Setup → Members → Patrol** (→ [Automate Recurring Work](#automate-recurring-work))
- Scheduled Slack posts (configured as a scheduled command combined with `workflows/chat_post_command`)
- The CLI: `guildbotics run <command_name> [args...]` (→ [Run on a Server](#run-on-a-server))

For all front matter options, context injection, and command composition, see the [Custom Command Development Guide](docs/custom_command_guide.en.md).

## Operations Reference

### Storing API Keys and Tokens

GuildBotics keeps secrets (LLM API keys and account tokens) out of plain text files whenever it can.

- **Encrypted store:** the OS keychain holds the encryption key. Values live in encrypted `.guildbotics/local/secrets.enc.json` files for the workspace and identities. The workspace only keeps a non-secret index, `.guildbotics/config/secrets.yml`, listing key names. This machine's keychain namespace is in `.guildbotics/local/secrets.json`. There is no `.env` secret backend.
- **Two stores:** the members' credentials (`{PERSON_ID}_...`) and the services' connections (`BRANVIL_ADMIN_TOKEN`) are kept in the identities' store, shared by every workspace on the machine (its index is `~/.guildbotics/identity/.guildbotics/config/secrets.yml`). The workspace's store keeps the other keys, such as LLM API keys. A process reads the identities' store first, then the workspace's.

- **Precedence:** real environment variables > encrypted store. GuildBotics does not read a workspace `.env`.
- **Exchange format:** `guildbotics secrets export` / `import` use dotenv only as a transfer file. `secrets set --from-file` absorbs a file (for example a PEM) into the encrypted store.

Put `KEY=value` entries in a UTF-8 import file. The values `$NAME` and `${VAR}` are stored literally. Apart from blank lines and comments, a line without `=` causes the entire file to be rejected before any value is written.

Manage secrets with the `guildbotics secrets` CLI (see the [CLI Reference](docs/cli_reference.md#guildbotics-secrets) for all subcommands and options):

```bash
guildbotics secrets status                        # OS secret-store availability and key count
guildbotics secrets export --file secrets.env     # export secrets for a move
guildbotics secrets import secrets.env            # import them on the new machine
guildbotics secrets --identities status           # the same, for the members' credentials
```

`--identities` before the subcommand makes it act on the identities' store; without it, the CLI refuses to store a member's key in a workspace. A member's key prefix is shared by every workspace, so two IDs that differ only in `-` and `_` (`foo-bar` and `foo_bar`) cannot both exist, even in different workspaces.

A workspace's secrets are stored per workspace (the encrypted file is tied to the `store_id` in the workspace's `.guildbotics/local/secrets.json`. A copy of the workspace that includes `local/` uses the same namespace as the original). Choose the target workspace with `--workspace` before the subcommand. Without it, the active workspace is required. `guildbotics secrets status` always shows where the target resolved on its `workspace:` line.

```bash
guildbotics secrets --workspace /path/to/workspace status
guildbotics secrets --workspace /path/to/workspace list
```

### Run on a Server

To move a workspace and its members to another Hub, transfer the configuration and credentials separately.

1. Set up a checkout on the target machine as in [Installation](#installation)
2. Copy the workspace folder and `~/.guildbotics/identity/` (the members' identities) to the target machine
3. Move the secrets: run `guildbotics secrets export --file ...` on the source and `guildbotics secrets import ...` on the target, and the same with `--identities` for the members' credentials (delete the export files afterwards). Keychain encryption key never leaves the machine
4. Build the isolated agent environment on the server and log in to the AI CLI tool there:

   ```bash
   guildbotics environment status                    # runtime, base image, environment, and logins on this machine
   guildbotics environment image load agent.tar      # only when the declaration names a base image: load the docker save archive built for this machine
   guildbotics environment build                     # build the environment the workspace declares
   guildbotics environment login codex               # log in inside the environment (device code flow)
   ```

   While the service runs, a changed declaration is rebuilt automatically; the login is kept per machine under `~/.guildbotics/data/agent_environment/`

**Starting and stopping the server and its service**:

```bash
guildbotics workspace status   # check the target workspace and its configuration
guildbotics server start       # run the server (add --services to start its services as well)
guildbotics server stop        # end the server after its in-flight work finishes
guildbotics service start      # start the services of the running server, as Run on the Service screen does
guildbotics service stop       # stop the services after their in-flight work; the server keeps running
```

`guildbotics server start` runs the server of the checkout in the terminal. Once it listens, it starts its services only with `--services`, or when **Start the services when the server starts** is on on the **Service** screen; otherwise the server runs alone and you start the services with `guildbotics service start` or on the screen. If the services fail to start, the server keeps running and shows the failure on the screens and in its output. The server runs until `guildbotics server stop` or a signal: the first Ctrl-C lets in-flight work finish, a second cancels it, and SIGTERM (what an OS sends to stop a service) cancels it at once. `guildbotics server stop` waits for the server to exit, however long its in-flight work takes; `guildbotics server stop --force` cancels the work at once, and force-terminates the server only when its teardown does not end in time. `guildbotics service start` and `guildbotics service stop [--force]` need a running server and fail with the reason when there is none. For the options, see the [CLI Reference](docs/cli_reference.md#guildbotics-server). `guildbotics server start` exits with the server's exit code, 0 when it was asked to end.

The services run with the settings saved on the **Service** screen, however they start: the sources to include, the patrol interval, and the failure limit (see [Start the Service and Ask for Work](#start-the-service-and-ask-for-work)). Open the screen with `guildbotics open`; a change applies from the next start of the service, and whether the server starts the services applies from its next start. Whether the machine stays awake while the service waits is on the same screen and applies at once. The settings are saved in the workspace's `.guildbotics/local/service.yml`; to choose them before the first start, write the file by hand (keys left out keep their defaults):

```yaml
start_with_server: false      # start the services when the server starts
keep_awake: false             # keep the machine awake while the service waits
scheduled: true               # scheduled commands
routine: true                 # patrol commands
event_queue: true             # event triggers (at least one of the three stays true)
routine_interval_minutes: 10  # 1-1440
max_consecutive_errors: 3     # 1-20
```

**Running commands** (equivalent to running from the **Edit Command** screen):

```bash
guildbotics run <command_name> [args...]
echo "Hello" | guildbotics run translate
```

Use `--person` or `<command>@<person_id>` to choose the member that runs it.

The server holds a lock for the machine root (`~/.guildbotics/data/run/app-api.lock`) for its entire lifetime, so only one server can use that root; a second one refuses to start. Services hold `service.lock`, including the keep-awake setting, only while running.

The server also serves the API and the screens, so `guildbotics run` can delegate commands to it. Stopping the services, on the **Service** screen or with `guildbotics service stop`, leaves the server serving the screens.

Completed work is kept in TaskRun records (`state/task-runs/`), so a service restart does not accept the same input again.


### Run the Server in a Terminal

Run `guildbotics server start` on the Hub. Leave that terminal open to see startup and service logs. Use `guildbotics server start --services` to start scheduled commands, ticket patrol, and chat reception at the same time.

To update from a newly built checkout, stop the server with `guildbotics server stop`. Build the screens and the Rust executable as in [Installation](#installation), run the new executable's `install` command, and start the server again.

For a macOS Hub that runs GuildBotics and Branvil at login, follow the [Hub installation and update guide](docs/hub-server.md).

### Use It from a Browser

The server serves GuildBotics' screens (service, commands, settings, diagnostics) at `http://127.0.0.1:8765/-/guildbotics/`, and you use them in a browser.

**On the machine where the server runs**:

```bash
guildbotics open
```

The browser opens with a single-use entry link that expires after 60 seconds. There is no sign-in screen: the OS user who can read `~/.guildbotics/data/server/<port>/owner.token` is the owner. A session lasts 30 days; when it has expired, run `guildbotics open` again.

**In a browser on another machine**: GuildBotics runs only on the Hub, and another machine reaches its screens over SSH. That machine needs no GuildBotics installation, only an OpenSSH client (see [What You Need](#what-you-need)).

1. Let the machine log in to the Hub with an SSH key. Add its public key (`~/.ssh/id_ed25519.pub`; create one with `ssh-keygen -t ed25519` if it has none) to the Hub's `~/.ssh/authorized_keys`. On the Hub:

   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\n' 'ssh-ed25519 AAAA... alice@laptop' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
   ```

2. Connect once with `ssh <Hub>` and accept the host key after comparing its fingerprint with the one the Hub prints for itself:

   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

3. Check that the Hub answers `guildbotics` over non-interactive SSH:

   ```bash
   ssh <Hub> guildbotics --version
   ```

   A non-interactive SSH session does not read the shell startup files an interactive login does, so a `guildbotics` installed under `~/.local/bin` is often invisible to it. The shortest fix is a link from a directory that is always on the PATH, made on the Hub:

   ```bash
   sudo ln -sf ~/.guildbotics/bin/guildbotics /usr/local/bin/guildbotics
   ```

   `~/.guildbotics/bin/guildbotics` is the managed binary. Re-run `guildbotics install` from a newly built binary to update it.

4. Forward the port. The forwarding service in the [Branvil Integration Guide](docs/branvil_integration.en.md#set-up-ssh-forwarding-on-another-machine) keeps it forwarded from login on; install it with `--port 8765` (and `--port 7878` as well when you use Branvil). For a single session, `ssh -L 8765:127.0.0.1:8765 <Hub>` also works.
5. Print an entry link on the Hub and paste it into your browser:

   ```bash
   ssh <Hub> guildbotics open --print
   ```

**Connect another machine's CLI**: run `guildbotics connect` on that machine. A consent screen opens in the browser; once the owner allows it, a token for that machine's CLI is issued and kept in that machine's OS keychain, and `guildbotics open` on that machine uses it (connecting the same machine again revokes its previous token).

What the screens do with files and folders:

- There is no "Browse" for folders: type the path on the Hub (a workspace can also be picked from the registered ones)
- Commands are edited in the screen's editor; they are not opened in an external editor
- A file dropped on a command's input, or an image pasted there, is uploaded as a copy (the agent's changes do not reach the original, and folders cannot be dropped). Uploaded files stay until the server restarts, and the manual runs started from the screens can read them
- Settings labelled "this machine" (keeping it awake, the registered workspaces) are the Hub's

**Port**: the server listens on the fixed port 8765. If another program uses that port, the server does not start. To use another port, set `GUILDBOTICS_SERVER_PORT` wherever the server and the CLI start; both read it (the service keeps the value it was installed with).

### Account-Related Environment Variables

**LLM API keys**:

- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

The member keys below (`{PERSON_ID}_...`) and `BRANVIL_ADMIN_TOKEN` are kept in the identities' store, shared by every workspace; the LLM API keys are kept in the workspace's store.

**Slack access** (per member, format: `{PERSON_ID}_...`):

- `{PERSON_ID}_SLACK_BOT_TOKEN`: Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: Slack App-Level Token


**Branvil access**:

- `BRANVIL_ADMIN_TOKEN`: the token Branvil issues GuildBotics when its owner allows the connection, one for every workspace; the host uses it to set up members and to open Branvil's GUI
- `{PERSON_ID}_BRANVIL_TOKEN`: the member's Branvil token, issued by GuildBotics when the member is saved and used in every workspace (its Branvil name is `account_info.branvil_username` in the member's `identity.yml`)
- Your own git's Branvil token: one owner token per machine, issued the first time your own git asks `guildbotics git-credential` for one and kept in that machine's OS keychain only, never shared (→ [Use Branvil from your own checkouts](docs/branvil_integration.en.md#use-branvil-from-your-own-checkouts))

GuildBotics reads the encrypted stores automatically. GuildBotics does not read a workspace `.env`.

### Workspace and Data Locations

The workspace in use is recorded in `~/.guildbotics/data/active-workspace.json`. From the CLI, inspect and change it with:

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
```

The workspace used by `guildbotics member` commands is resolved in this order:

1. `--workspace <dir>` given before the subcommand
2. An explicit `GUILDBOTICS_WORKSPACE_ROOT` or `GUILDBOTICS_CONFIG_DIR` that points at `<workspace>/.guildbotics/config`
3. The workspace in use as recorded by the screens or `guildbotics workspace use`

The process cwd is never treated as a workspace. From the selected workspace, `GUILDBOTICS_CONFIG_DIR` is set to `<workspace>/.guildbotics/config`.

GuildBotics stores three kinds of local data:

- Machine-wide management information — the workspace in use, the server’s selection, and so on — is stored in `$HOME/.guildbotics/data`
- The members' identities, shared by every workspace, are stored in `$HOME/.guildbotics/identity/.guildbotics`, laid out like a workspace's `.guildbotics`
- Workspace state — memory, chat control, task-run records, interactive session records, activity events — is stored in `<workspace>/.guildbotics/state`
- Device-local data — diagnostics, transcripts, chat cache, member clones, the built agent environment (`local/agent_environment/snapshots`) — is stored in `<workspace>/.guildbotics/local`
- The isolated agent environment's runtime and its images live in `$HOME/.guildbotics/data/msb`; the AI CLI tools' logins and sessions, shared by every member and workspace on the machine, live in `$HOME/.guildbotics/data/agent_environment/<tool>/`

### Configuration Files

**Project settings** (`team/project.yml`):

- `name`: project name
- `description`: short project description used as agent context
- `language`: project language
- `services.ticket_service`: ticket service settings
- `services.code_hosting_service`: code hosting service settings (the Branvil workspace used for repository operations)
- With Branvil, both services are `name: branvil` with a `workspace` (members write only to its repositories and take tickets from its board). Where Branvil is reached is part of the connection, shared by every workspace (→ [Branvil Integration Guide](docs/branvil_integration.en.md))
- `services.chat_service`: chat service settings (`name: slack`; `base_url` only when Slack is reached through a proxy)

**Member identity** (`~/.guildbotics/identity/.guildbotics/config/team/members/<person_id>/identity.yml`, the same in every workspace; the avatar sits beside it):

- `person_id`: unique identifier (lowercase alphanumerics, `-`, `_` only). It cannot be changed
- `name`: display name
- `person_type`: `human` for the human member, `agent` for an AI member
- `account_info`: account names with each service (`git_user`, `git_email`, `slack_user_id`, `branvil_username`)
- `profile.character`: profile such as interests, preferences, and conversation participation policy
- `speaking_style`, `relationships`

**Member participation** (`team/members/<person_id>/member.yml` in the workspace):

- `is_active`: whether the member runs as an AI agent
- `profile.roles`: role assignment
- `routine_commands`: override the default patrol commands
- `task_schedules`: cron-based scheduled commands
- `message_channels`: watched channel settings (`chat.enabled`, `channel_id`/`name`), on the project's chat service

A field in the other file is refused when the team is loaded. A workspace member whose identity is missing (the member was deleted from another workspace) is left out of the team, and the rest of the team works, until it is removed from this workspace.

**LLM / AI CLI tool settings**:

- `intelligences/brain_mapping.yml`: per-function engine and LLM / AI CLI slot assignments. For `chat_decision`, which decides whether a chat needs a response agent, see the [Slack integration guide](docs/slack_integration.en.md#chat-judgment-engine).
- `intelligences/cli_agent_mapping.yml`: default AI CLI tool selection
- `intelligences/cli_agent_filesystem_grants.yml`: the directories under the home directory AI CLI tools may use beyond their working directory (`documents`), shared by every member of the workspace. Apart from this file, `Documents/GuildBotics` (the exchange directory) is always granted read/write: what agents make for you goes there. Device-only extra paths and denies go in `local/cli_agent_filesystem_grants.yml` (see [Native agent runtime](docs/native_agent_runtime.en.md))
- `intelligences/cli_agents/<tool>/*.yml`: the parameters and effort mapping for each AI CLI tool. Only Codex, Claude Code, Grok Build, GitHub Copilot CLI, and Antigravity CLI can be run; supporting another tool means implementing a native adapter in the GuildBotics repository
- `intelligences/agent_environment.yml`: the environment shared by every member and slot: its base image (`image`: GuildBotics' own Debian image unless set; for another toolchain, build an image yourself that keeps git, load it on the machine with `docker save` → `guildbotics environment image load`, and pick it in the settings or declare it with `guildbotics environment image declare`; images and declared digests are per CPU architecture), resources assigned to each microVM (`resources.memory_mib`, default 4096; `resources.cpus`, default 2), network policy (`network`: `deny`, an `allowlist`, or an explicitly selected `unrestricted`; omission means `deny`), and DNS resolvers (`dns.nameservers`: fixed IPv4 addresses or `host`). Put additional tools in the image rather than a package list. `docker/agent-environment/Dockerfile` and `scripts/build-agent-environment-image.sh` are the example used to develop GuildBotics itself. Changing the image rebuilds the environment; resources, network, and DNS apply at the next microVM boot without rebuilding the disk snapshot
- `team/members/<person_id>/intelligences/`: optional per-member overrides. By default they inherit the team settings

For the available values and security considerations, see [Native Agent Runtime](docs/native_agent_runtime.en.md#configuration).

### CLI Reference

For the complete list of CLI commands and options, see the [CLI Reference](docs/cli_reference.md), which is generated from the source code.

## Troubleshooting

| Symptom | What to check first |
| --- | --- |
| `guildbotics` command not found | Run `~/.guildbotics/bin/guildbotics` and check `~/.local/bin` in PATH. If neither exists, install them again (→ [Installation](#installation)) |
| The server does not start, or has stopped | Read the output of `guildbotics server start` (see [Run the Server in a Terminal](#run-the-server-in-a-terminal)) |
| Not sure which workspace is in use | Check and change it under **Setup → Project**. From the CLI, use `guildbotics workspace status` / `guildbotics workspace use <path>` |
| A member does not work, or the configuration looks wrong | Validate the LLM, AI CLI tool, Branvil, and Slack settings under **Setup → Verification** |
| Slack events are not received | Check Socket Mode, the App-Level Token, and the bot events, and whether **Event triggers** is included on the **Service** screen (every start of the services uses those settings) |
| A command execution failed | Open the session on the **Diagnostics** screen and read the logs. You can also ask the AI assistant to investigate the cause |
| The scheduler stopped | The worker stops when **Stop after consecutive failures** (default: 3) is reached. Check the failure on the **Diagnostics** screen before restarting |
| AI CLI turns do not start, or an alert says this device cannot run them | Open **Setup → Agent execution environment** and check **Status on this device** (or run `guildbotics environment status`). The runtime needs hardware virtualization (Apple Silicon), the environment must be **Ready** (press **Build**; a changed declaration rebuilds by itself while the service runs), and credentials must be saved for the tool on this machine (use the login command shown on the screen). Saved credentials do not guarantee successful authentication. While the environment is building, work is deferred, not failed |
| AI CLI authentication fails despite saved credentials | In **Setup → Agent execution environment**, copy the login command shown for the tool, run it in a terminal, and wait for the automatic status update (every 10 seconds while the screen is open). Press **Refresh display** to reread saved state. Use **Check again**, when available, to retry usage retrieval. The command uses the managed CLI's absolute path. **Credentials saved** only confirms that a file exists (for Claude Code and Grok Build, that the login sealed under this machine's keychain opens; these logins never enter a turn's microVM, and an earlier plain-file login is not read, so log in once more after updating and delete the old files — see [the details](docs/native_agent_runtime.en.md#logins-kept-outside-the-microvm)). An authentication failure is shared by all members using that tool on this device; completing login or a later successful turn clears it. Network errors and rate limits do not change this authentication result, and a past failure does not block retries |

When an AI CLI usage refresh fails, the top notification area and the tool's settings row show the same problem. The row includes the last-check time. **Check again** bypasses the usage cache for that tool, and **Error details** opens the corresponding diagnostics session. A successful usage refresh clears the notification and the row's warning. The notification does not by itself mean credentials have expired; network errors and unavailable usage panels also prevent retrieval. **Refresh display** rereads saved state without probing the tools. On the Activity screen, each tool is read on its own and shared by every member using it, so a slow tool never delays another. The last successful reading stays visible with its time while a newer one is fetched; if that fetch fails, the reading is marked as the previous value and no longer drives the rate-limit badge, and the tool is retried after 30 seconds instead of the usual 5 minutes.

**Diagnostics logs**: a searchable execution summary is recorded in `<workspace>/.guildbotics/local/run/diagnostics.jsonl`, and the full events, logs, spans, and inputs/outputs are stored per execution as JSONL under `run/sessions/`. The **Diagnostics** screen shows both the execution history and the latest global / system session. For a restricted AI CLI turn, `agent_environment.network_egress_candidate` records explicit URLs and host-and-port pairs from structured command/tool/runtime failure evidence after excluding known allowed domains and allowed local IP addresses. It is an informational clue rather than proof of a microsandbox denial; successful command output, bare hostnames/IP addresses, extra DEBUG logging, and agent self-report are not used.

**Debug output**: environment variables for more verbose logging:

- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `AGNO_DEBUG`: extra debug output from the Agno engine (`true`/`false`)

Transcript detail (`standard` / `full`) and retention days are configured from the **Diagnostics** screen and stored in `.guildbotics/config/transcripts.yml`.
