<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

GuildBotics lets you run AI CLI tools such as Claude Code or Codex as team members who keep working with your development team. Each member has its own name, GitHub / Slack account, roles, and memory. The actual investigation, coding, and judgment are delegated to the AI CLI tool, while every external action the member takes (commits, PR creation, comments, Slack posts, memory writes) goes through a single dedicated CLI (`guildbotics member`), where it is executed and recorded.

There are two ways to work with the same member:

- **Work together** — invoke the member in your Claude Code or Codex session and pair-program in the repository you currently have open (→ [Work Together with a Member](#work-together-with-a-member))
- **Delegate** — ask through a GitHub Projects ticket or a Slack message, and the member investigates, implements, opens the pull request, and replies on its own (→ [Delegate GitHub Tickets](#delegate-github-tickets) / [Ask for Work in Slack](#ask-for-work-in-slack))

Both ways drive the same member — same identity, same memory. What you teach while working together is kept as memory and carries over to the work you delegate.

You configure, run, and monitor GuildBotics with the GuildBotics Desktop app (GUI). Configuration is stored as plain text files inside the workspace, so you can move it to a server without a GUI and operate it with the `guildbotics` CLI alone (→ [Run on a Server](#run-on-a-server)).

---

## Important Notice (Disclaimer)

- This software is in alpha version. There is a very high possibility of breaking incompatible changes in the future, and malfunctions are expected to occur frequently, so use in production environments is not recommended.
- The author and distributor assume no responsibility for malfunctions of this software or damages caused thereby. In particular, due to malfunctions or runaway of AI agents, there is a possibility of fatal destruction to the system in use or external services, data loss, and leakage of confidential data. Use at your own risk and verify in an isolated test environment.

---

- [What You Can Do](#what-you-can-do)
- [Getting Started](#getting-started)
- [Work Together with a Member](#work-together-with-a-member)
- [Delegate GitHub Tickets](#delegate-github-tickets)
- [Ask for Work in Slack](#ask-for-work-in-slack)
- [Automate Recurring Work](#automate-recurring-work)
- [Create Your Own Commands](#create-your-own-commands)
- [Operations Reference](#operations-reference)
- [Troubleshooting](#troubleshooting)

---

## What You Can Do

- **Multiple members**: Define multiple AI members with distinct roles, personalities, and memory (the identifier in configuration files is `person`)
- **GitHub integration**: Ticket management via GitHub Projects / Issues, plus PR creation, comments, and review handling by members
- **Slack integration**: Members watch configured channels and handle the requests they receive there as themselves
- **Member memory**: Personal and team memory that members recall and maintain across sessions
- **Interactive member sessions**: The guildbotics skill lets an AI CLI tool work as a member in your current repository
- **Scheduled execution**: Per-member patrol commands and cron-based scheduled commands
- **Custom commands**: Define your own work as a Markdown prompt / Python / Shell / YAML command and reuse it per member or role
- **Swappable LLM / AI CLI tool**: Swap LLM providers or delegate to an AI CLI tool (Codex, Claude Code, Grok Build, Antigravity, and other AI tools launched from a local CLI)
- **Desktop AI assistants**: Ask questions and apply proposed source changes in the command editor, and investigate execution failures in the diagnostics screen
- **Internationalization**: English / Japanese

## Getting Started

### What You Need

- **OS**: Linux (verified on Ubuntu 24.04) or macOS (verified on Sequoia)
  - The desktop app supports macOS Apple Silicon (arm64) and Linux x86_64
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**: used to build GuildBotics and install the CLI
- **An LLM API key** (obtain one of the following in advance):
  - Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey)
  - OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys)
  - Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys)
- **An AI CLI tool** (install one of the following in advance, launch it once, and complete authentication):
  - [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)
  - [OpenAI Codex CLI](https://github.com/openai/codex/)
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (requires a Claude Pro or Max subscription)
  - [Grok Build](https://docs.x.ai/build/overview)
  - [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli)

With Codex, Claude Code, Grok Build, GitHub Copilot CLI, or Antigravity CLI, a member can carry a session over and resume where it left off. For authentication, how sessions are bound to Slack threads and tickets, and how execution permissions are configured, see [Native Agent Runtime](docs/native_agent_runtime.en.md).

### Installation

There is no general-purpose installer for the desktop app yet. Clone the repository and build it locally. In addition to uv, the build requires **Node.js 24 or later** and **Rust (rustup) stable 1.88 or later** (plus the WebKitGTK 4.1 development package on Linux). For prerequisites and build / install steps, see [desktop/README.md](desktop/README.md) (the "1. 前提ツール" section lists the required tools).

On first launch, the desktop app installs the following:

- `~/.guildbotics/bin/guildbotics`: the managed GuildBotics CLI used by AI CLI tools and skills
- `~/.local/bin/guildbotics`: a small shim that forwards to the CLI above. It is only written when missing or when it is an existing managed shim
- The GuildBotics skill under the user skill directory of each detected Codex / Claude Code / Grok Build / Antigravity CLI / GitHub Copilot CLI. Skills you created or edited are never overwritten

In environments without the desktop app (headless servers and the like), install the CLI on its own with `uv tool install guildbotics` (→ [Run on a Server](#run-on-a-server)).

### Initial Setup

Launching the desktop app opens **Project** setup, where you configure:

- The agent default language (English / Japanese), used for command and role definitions and for LLM output instructions
- The workspace folder
- The project description
- Whether to use GitHub integration

**Integrating with GitHub or Slack requires preparation on the service side before you start configuring.** Both integrations are optional and can be enabled later from the **Setup** screen. If you just want to see GuildBotics run, answer "Do not use GitHub" and move on.

- **GitHub**: a valid GitHub Project URL is required to finish the initial setup. Do [Create a GitHub Project](#create-a-github-project) (with the Todo / In Progress / Done statuses) and [Prepare a GitHub Account for the AI Agent](#prepare-a-github-account-for-the-ai-agent) (pick an account type and issue the token) first. Assigning a GitHub account to a member also requires that member's username, git email address, and credentials
- **Slack**: for members to take requests in Slack, you need a Socket Mode Slack App and its bot / app tokens. See [Ask for Work in Slack](#ask-for-work-in-slack) for what to prepare

In GuildBotics, the folder you choose as the project's working location is called the **workspace**. Plain text configuration files are written there:

- `.env`: environment variable settings (non-secret settings such as the log level)
- `.guildbotics/config/secrets.yml`: index of key names stored in the OS keychain (never the values)
- `.guildbotics/config/team/project.yml`: project definition
- `.guildbotics/config/intelligences/`: LLM and AI CLI tool settings

API keys and account tokens are stored in the OS keychain when one is available, and only their key names are recorded in the files above (→ [Storing API Keys and Tokens](#storing-api-keys-and-tokens)).

After project setup, configure the following in the desktop app:

- **LLM / AI CLI tools**: default LLM, AI CLI tool selection, and LLM API keys
- **Members**: add and configure team members (assigning a GitHub account needs the credentials above)
- **GitHub**: task board settings (only when you use GitHub; the GitHub Project URL itself is entered in the **Project** section). Configure the [lane mapping](#task-board-conventions) when you use your own status names, and the `Agent` field
- **Verification**: press **Validate settings** to run a read-only check across LLM, AI CLI tool, GitHub, Slack, and Git. It does not update GitHub or Slack data

### Quick Start

Confirm that setup is complete by running a custom command. Open the **Edit Command** screen in the desktop app.

The initial setup placed sample commands in `.guildbotics/config/commands/` under your workspace. This walkthrough uses the `translate` sample.

1. Pick `translate` from the command list.

2. Type `Hello` into **Input text** in the run panel and press **Save and run**.

You are set if the translated text appears under **Output**. The default LLM you configured was called, and your custom command works.

You can assign hotkeys to commands you use every day.

- **Setup → Shortcuts**: a hotkey that opens the **Quick run window**. Copy the text you want to translate, press the key, and run it as the input text
- **The hotkey chip in the Edit Command screen's command bar**: runs that command directly. The quick run window opens instead when an input is missing

### What Next

- Pair-program with a member in the repository you have open → [Work Together with a Member](#work-together-with-a-member)
- Delegate everything from ticket to pull request → [Delegate GitHub Tickets](#delegate-github-tickets)
- Have a member handle requests in Slack → [Ask for Work in Slack](#ask-for-work-in-slack)
- Automate recurring work or build your own commands → [Automate Recurring Work](#automate-recurring-work) / [Create Your Own Commands](#create-your-own-commands)

## Work Together with a Member

The first launch of the desktop app installs the **guildbotics skill** into the user skill directory of each detected AI CLI tool, such as Claude Code or Codex (→ [Installation](#installation)). That skill is what lets you work together with a member in the repository you currently have open.

The skill lives in the tool's user configuration directory (`~/.claude`, `~/.codex`, and so on), so the same skill is used whether you start the tool from its CLI or from its app. To check that it is installed, see each tool's skill status under **Setup → LLM / AI CLI tools** in the desktop app.

There are two prerequisites:

- You have launched the desktop app once (this installs the guildbotics skill and the managed CLI)
- A member is configured

Start your AI CLI tool in the repository you want to work in and ask for work, **naming the skill and the member**. That phrase is what invokes the skill.

```text
Use the guildbotics skill and commit and push this change as the member alice
```

The AI CLI tool loads the member's profile (roles, judgment criteria, speaking style) and memory, and responds as that member from then on. External actions such as commits, pushes, PR creation, and Slack posts are performed under the member's own identity.

If you say "remember this" during the session, it is saved as the member's memory. That memory is also used when you delegate work through tickets or Slack.

## Delegate GitHub Tickets

This is the flow where you ask a member for work through a GitHub Projects ticket and delegate investigation, implementation, and PR creation (using the default `ticket_driven_workflow`).

**Note**: GitHub integration is optional. Without it you can still use the Slack chat workflow and automate commands on a schedule.

### What It Does

- **Assign work on the task board**: pick the member in the ticket's `Agent` field and move the ticket to the ready lane, and the member runs that task
- **Check the results**: when a task completes, the member leaves the result as a comment, a PR, a review reply, or a reaction
- **Create pull requests**: when code changes are needed, the member publishes a working branch and creates or reuses a pull request
- **Create tickets**: when asked to file a follow-up ticket, the member creates a real issue in the repository

### Create a GitHub Project

Create a GitHub Projects (v2) project and add the following columns (statuses) up front:

- Todo (ready)
- In Progress
- Done

If you want to keep the status names of an existing project, map them with the lane mapping described below.

### Prepare a GitHub Account for the AI Agent

Prepare an account the member uses to access GitHub. Any of the following works:

- **Machine account** (machine user)
  - Recommended if you want the feel of "working with an AI agent through the task board and pull requests". Note that under [GitHub's Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements), each user may create only one free machine account.
- **GitHub App**
  - There is no limit on the number of accounts, but a GitHub App cannot access a GitHub Project owned by a **personal** account. GitHub also labels it as a bot, which takes away some of the atmosphere.
- **Proxy agent** (use your own account for the AI agent)
  - The simplest option. With this approach it looks less like working with an AI agent and more like talking to yourself.

**Using a machine account**:

1. Add the machine account you created to the Project and the repository as a collaborator
2. Issue a **Classic** PAT (Personal Access Token) with both the `repo` and `project` scopes

**Using a GitHub App**:

The member edit screen in the desktop app (select "GitHub Apps" on the GitHub tab) offers a "Register a new app" mode, which creates and installs the app on GitHub semi-automatically. You only approve the creation and pick the repositories in the browser; the App ID, private key, and installation ID are then filled in automatically, so the manual steps below are not needed.

When creating the GitHub App manually, configure the following permissions.

- **Repository permissions**: **Contents** / **Issues** / **Projects** / **Pull requests** as Read & Write
- **Organization permissions**: **Projects** as Read & Write

After creating the GitHub App:

1. Use "Generate a private key" on the GitHub App settings page to download and store the `.pem` file
2. Install the app to your repository / organization from "Install App" and obtain the **installation ID**. The trailing number of the URL shown after installation (`.../settings/installations/<installation ID>`) is the installation ID. Keep it — you need it during configuration

**Using a proxy agent**:

Issue a **Classic** PAT for your own account as well, with both the `repo` and `project` scopes.

### Prepare Credentials and the Execution Environment

- Register each member's GitHub credentials (the PAT, or the GitHub App settings) in GuildBotics from member settings in the desktop app. Writes to GitHub / git use the assigned member's credentials, not your local `gh auth` user
- Ticket-driven work happens in a per-member working directory (default: `<workspace>/.guildbotics/data/workspaces/<person_id>`). Cloning, pushing, PR creation, and comments are executed by the member itself through the `guildbotics member` CLI
- If you also use the AI CLI tool interactively, we recommend denying or requiring approval for `gh`, direct token / API writes, and `git push`. This is a guardrail against falling back to your own GitHub account, not a sandbox that fully contains token leakage
- When using Codex as the AI CLI tool, check the Codex CLI's authentication and network reachability:

  ```bash
  codex doctor
  ```

### Validate the Configuration

Press **Validate settings** under **Setup → Verification** in the desktop app to check whether each active member's LLM, AI CLI tool, GitHub, Slack, and Git settings actually work (GitHub and Slack are accessed read-only).

### Task Board Conventions

**Custom fields**: GuildBotics manages an `Agent` field that selects the member who runs a task. It is created automatically the first time GuildBotics touches the GitHub Project, so no explicit setup is required. Because a GitHub App member cannot be a GitHub assignee, use this field to pick the member.

**Lane mapping**: GuildBotics treats GitHub Projects statuses as three lanes (ready / working / done).

- By default `Todo` is ready, `In Progress` is working, and `Done` is done. A standard board needs no configuration
- Statuses **between** ready and done (for example `In Review`) are automatically treated as working
- Statuses **before** ready (for example `Backlog`) and **at or after** done (for example `Icebox`) are ignored. Intermediate or holding lanes can be added just by ordering the board columns
- If you use your own status names, map them under **Setup → GitHub** → **Lane mapping** in the desktop app. Without the GUI, set `services.ticket_manager.lane_map` in `team/project.yml` (→ [Configuration Files](#configuration-files))

### Start the Service and Ask for Work

Open the **Service** screen in the desktop app and press **Run**. A worker starts for each active member and runs the work of the selected sources in order.

The screen has three execution sources, each toggled individually with **Include in service run** (only while the service is stopped).

- **Patrol commands**: runs each member's patrol commands once per **Patrol interval (minutes)**. A new member with a GitHub account gets the ticket-driven workflow as its default; a member without one starts with no patrol commands, so pick them under **Setup → Members → Patrol**
- **Scheduled commands**: runs the scheduled commands defined in member settings at the times you specify
- **Event triggers**: receives events such as Slack messages and routes them to the chat workflow

To use the ticket-driven workflow, start the service with **Patrol commands** included. When **Stop after consecutive failures** (default 3) is reached, that member's worker stops.

You can follow what is happening in the events and logs of the **Global / system** session on the **Diagnostics** screen, and per-member results are collected on the **Activity** screen.

To ask for work, use a GitHub Projects ticket as follows:

1. Create a ticket, select the target Git repository, and save it as an issue
2. Write the instructions for the member in the ticket (this becomes the prompt, so be as specific as possible)
3. Select the member in the `Agent` field
4. Move the ticket to the ready lane

Once work starts, you interact with the member like this:

- When the member has a question, it posts it as a ticket comment. Answer in a ticket comment and the member picks up your answer on its next check and continues
- When the task completes, the member leaves a comment / PR URL / review reply / reaction
- Review the PR created from the ticket on the PR itself. Unresolved review threads are delegated back to the member in charge

To stop, press **Stop**. The service stops accepting new work and exits after in-flight work finishes. If you do not want to wait, press **Force stop** to cancel the in-flight work.

To start and stop the service from the CLI, see [Run on a Server](#run-on-a-server).

## Ask for Work in Slack

A member watches the Slack channels you configure and handles the requests it receives there as itself. Ask something like "please handle the review comments on this PR" and the member identifies the target repository, prepares a working branch, actually investigates, edits code, and performs the GitHub operations, then reports the result back in Slack.

How it behaves in Slack (reply with a message / only add a reaction / do nothing / ask a clarifying question / report why it is blocked) is decided by the member (the AI CLI tool) from the message content, its own role and profile, and the per-channel participation policy. It does not force a reply into small talk or into conversations where its role adds nothing.

Two things need to be configured:

1. **Create a Slack App**: under **Setup → Members → Slack** in the desktop app, choose **Register a new app** and press **Create the app on Slack**. The browser opens with the required scopes, Socket Mode and event subscriptions already configured, so you pick a workspace and create the app. Then press **Reinstall to Workspace** under **OAuth & Permissions** and copy the two tokens (`xoxb-...` / `xapp-...`) — the first bot token Slack issues does not carry the scopes, so the reinstall is required
2. **Register it with the member**: on the same Slack tab, configure the following
   - Paste the **Slack Bot token** (`xoxb-...`) and **Slack App token** (`xapp-...`), then press **Verify app** to confirm the bot name, workspace, granted scopes, and channel membership
   - **Channel**: add the channel names or IDs to watch
   - **When to join**: choose Join actively / Join when needed / Mentions only

If you already have a Slack App, choose **Use a registered app** and start from pasting the tokens.

These settings are stored in the member's `person.yml` (`message_channels`).

To receive chat events, start the service with **Event triggers** included on the **Service** screen. Scheduled posts at fixed times (a news digest, for example) go through a separate path: configure `workflows/chat_post_command` as a scheduled command.

For the manual Slack App setup steps (including the list of required scopes), sharing one connection across members, the details of thread participation policies, and scheduled post examples, see the [Slack Integration Guide](docs/slack_integration.en.md).

## Automate Recurring Work

Configure two kinds of automated execution per member under **Setup → Members → Patrol** in the desktop app. Both are run by the service you start on the **Service** screen.

- **Patrol commands**: while the service is running, these run repeatedly in round-robin order once per **Patrol interval (minutes)** (for example, patrolling the task board). Turn on "Configure patrol commands for this member" and choose the saved commands. Members with it turned off do not run patrol work
- **Scheduled commands**: use **Add schedule** to choose a command and its execution time (for example, periodic reports or cleanup). The time can be an Hourly / Daily / Weekly preset, or a **Detailed schedule** (five-field cron notation)

The patrol interval and the number of consecutive failures that stops a worker are set on the **Service** screen.

These settings are stored in the member's `person.yml`. On a server without the GUI, edit that file directly.

```yaml
person_id: alice
name: Alice
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

Like the `translate` command you ran in the quick start, you can define your own commands and use them for manual runs, scheduled runs, and scheduled Slack posts. You author and try them out on the **Edit Command** screen in the desktop app.

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

`functions/get_os_ui_language` is a built-in command, so no helper file has to be placed in the workspace.

**How to run a command**:

- **Run** on the **Edit Command** screen (you can supply input text and additional args)
- The **Quick run window** opened with a hotkey
- Patrol / scheduled execution under **Setup → Members → Patrol** (→ [Automate Recurring Work](#automate-recurring-work))
- Scheduled Slack posts (configured as a scheduled command combined with `workflows/chat_post_command`)
- The CLI: `guildbotics run <command_name> [args...]` (→ [Run on a Server](#run-on-a-server))

For all front matter options, context injection, and command composition, see the [Custom Command Development Guide](docs/custom_command_guide.en.md).

## Operations Reference

### Storing API Keys and Tokens

GuildBotics keeps secrets (LLM API keys and account tokens) out of plain text files whenever it can.

- **OS keychain (default for new workspaces):** when a keychain is available (macOS Keychain, Windows Credential Manager, Linux Secret Service such as GNOME Keyring), setup stores secret values there. The workspace only keeps a non-secret index file, `.guildbotics/config/secrets.yml`, listing the stored key names.
- **`.env` backend:** workspaces without that index file (for example one created on a machine without a keychain) use the workspace `.env`. This is the supported approach for headless servers and CI. The `.env` file GuildBotics writes is owner read/write only (`0600`).
- **Precedence:** real environment variables > OS keychain > `.env`. In server operation, injecting environment variables always wins regardless of the backend.
- **GitHub App private key:** saving member settings in a keychain-backed workspace copies the contents of the PEM file referenced by `*_GITHUB_PRIVATE_KEY_PATH` into the keychain and removes the path entry from `.env`. The keychain copy replaces the file, so all that remains is deleting the plaintext `.pem` yourself. Unlike other secrets, the key material is never exposed in environment variables; it is read from the keychain only at the moment a GitHub App token is issued, so AI CLI tool child processes never see it.
- **`GUILDBOTICS_SECRETS_BACKEND`:** set to `keyring` or `env-file` to force a backend for that process only (for CI and scripted environments).

Manage secrets with the `guildbotics secrets` CLI (see the [CLI Reference](docs/cli_reference.md#guildbotics-secrets) for all subcommands and options):

```bash
guildbotics secrets status                        # backend in use and keychain availability
guildbotics secrets export --file secrets.env     # export secrets for a move
guildbotics secrets import secrets.env            # import them on the new machine
```

Secrets are stored per workspace (keychain entries are namespaced by the `store_id` in `secrets.yml`). Choose the target workspace with `--workspace` before the subcommand. Without it, the workspace in the current directory is used, or the active workspace when there is none. `guildbotics secrets status` always shows where the target resolved on its `workspace:` line.

```bash
guildbotics secrets --workspace /path/to/workspace status
guildbotics secrets --workspace /path/to/workspace list
```

### Run on a Server

All non-secret configuration is stored as plain text files inside the workspace, so once you have set things up you can move them to an environment without a GUI (a headless server, for example) and operate with the CLI alone.

1. Install the CLI on the target machine with `uv tool install guildbotics`
2. Copy the workspace folder to the target machine
3. Move the secrets: run `guildbotics secrets export --file ...` on the source and `guildbotics secrets import ...` on the target (delete the export file afterwards). Keychain entries themselves never leave the machine
4. On servers without a keychain, store secrets in `.env` or pass them as environment variables

**Starting and stopping the service** (equivalent to the **Service** screen in the desktop app):

```bash
guildbotics workspace status   # check the target workspace and its configuration
guildbotics start              # start the service
guildbotics stop               # stop accepting new work and exit after in-flight work finishes
guildbotics kill               # force stop immediately
```

Running `guildbotics stop` a second time cancels the in-flight work (equivalent to **Force stop** in the GUI). For options such as `--timeout` and `--force`, see the [CLI Reference](docs/cli_reference.md#guildbotics-stop).

By default `guildbotics start` starts both the member workers (patrol / scheduled / queued events) and the event listener. `--only` narrows what runs, but the member workers start in either case.

- `--only scheduler`: runs only patrol and scheduled commands; the event listener does not start (no Slack events are received)
- `--only events`: only receives events and runs queued events; patrol and scheduled commands do not run

**Running commands** (equivalent to running from the **Edit Command** screen in the desktop app):

```bash
guildbotics run <command_name> [args...]
echo "Hello" | guildbotics run translate
```

Use `--person` or `<command>@<person_id>` to choose the member that runs it.

The CLI and the desktop app share a lock file (`~/.guildbotics/data/run/service.lock`), so the service can never start twice on the same machine.

### Account-Related Environment Variables

**LLM API keys**:

- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack access** (per member, format: `{PERSON_ID}_...`):

- `{PERSON_ID}_SLACK_BOT_TOKEN`: Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: Slack App-Level Token

**GitHub access** (per member, format: `{PERSON_ID}_...`):

- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: PAT for a machine account / proxy agent
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: for a GitHub App

A `.env` file in the current directory is loaded automatically. Secrets stored in the OS keychain are loaded automatically as well.

When running without the desktop app, `GUILDBOTICS_ENV_FILE` pointing at an absolute `.env` path, or the `.env` in the current directory, is the fallback. `guildbotics start` and the desktop runtime also set `GUILDBOTICS_ENV_FILE` automatically when they load a workspace `.env`.

### Workspace and Data Locations

The workspace in use is recorded in `~/.guildbotics/data/active-workspace.json`. From the CLI, inspect and change it with:

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
```

The workspace used by `guildbotics member` commands is resolved in this order:

1. `--workspace <dir>` given before the subcommand
2. The workspace you are running inside, when it is already configured (an explicit `GUILDBOTICS_CONFIG_DIR`, or `.guildbotics/config` in the current directory)
3. The workspace in use as recorded by the desktop app or `guildbotics workspace use`

From the selected workspace, `GUILDBOTICS_CONFIG_DIR` is set to `<workspace>/.guildbotics/config`, and `GUILDBOTICS_ENV_FILE` is set too when `<workspace>/.env` exists.

GuildBotics stores two kinds of local data:

- Machine-wide management information — the workspace in use, the CLI scheduler PID, and so on — is stored in `$HOME/.guildbotics/data`
- Per-workspace runtime data — per-member working directories, task and chat execution records, diagnostics logs, prompt transcripts, and chat state — is stored by default in `<workspace>/.guildbotics/data`

You can change where per-workspace runtime data is stored by setting `GUILDBOTICS_DATA_DIR` in the workspace `.env`. If `GUILDBOTICS_DATA_DIR` is present in the environment at startup and the workspace `.env` does not set it, it is used as the shared runtime data location for that running process.

### Configuration Files

**Project settings** (`team/project.yml`):

- `name`: project name
- `description`: short project description used as agent context
- `language`: project language
- `repositories`: repository definitions
- `services.ticket_manager`: GitHub Projects settings
- `services.ticket_manager.lane_map`: maps the ready / working / done lanes to GitHub Project status names. Set this when your Project uses its own status names
- `services.code_hosting_service`: GitHub repository settings

**Member settings** (`team/members/<person_id>/person.yml`):

- `person_id`: unique identifier (lowercase alphanumerics, `-`, `_` only)
- `name`: display name
- `is_active`: whether the member runs as an AI agent
- `profile.roles`: role assignment
- `routine_commands`: override the default patrol commands
- `task_schedules`: cron-based scheduled commands
- `message_channels`: watched channel settings (`chat.enabled`, `chat.event_source=socket_mode`, `channel_id`/`name`)
- `profile.character`: profile such as interests, preferences, and conversation participation policy

**LLM / AI CLI tool settings**:

- `intelligences/cli_agent_mapping.yml`: default AI CLI tool selection
- `intelligences/native_agent_policy.yml`: filesystem access scope for Codex, Grok Build, and GitHub Copilot CLI (`workspace` or `host`). It is created during setup of a new workspace and configured under **LLM / AI CLI tools → Advanced settings** in the desktop app, or by editing the file directly in environments without a screen. Network access and the no-confirmation execution mode are fixed inside the GuildBotics integration
- `intelligences/cli_agents/<tool>/*.yml`: the effort mapping for each AI CLI tool. Only Codex, Claude Code, Grok Build, GitHub Copilot CLI, and Antigravity CLI can be run; supporting another tool means implementing a native adapter in the GuildBotics repository
- `team/members/<person_id>/intelligences/`: optional per-member overrides, including the execution permissions for Codex, Grok Build, and GitHub Copilot CLI. By default they inherit the team settings

For the available values and security considerations, see [Native Agent Runtime](docs/native_agent_runtime.en.md#configuration).

### CLI Reference

For the complete list of CLI commands and options, see the [CLI Reference](docs/cli_reference.md), which is generated from the source code.

## Troubleshooting

| Symptom | What to check first |
| --- | --- |
| `guildbotics` command not found | Run `~/.guildbotics/bin/guildbotics` with its absolute path. Also check that `~/.local/bin` is on your PATH |
| Not sure which workspace is in use | Check and change it under **Setup → Project** in the desktop app. From the CLI, use `guildbotics workspace status` / `guildbotics workspace use <path>` |
| A member does not work, or the configuration looks wrong | Validate the LLM, AI CLI tool, GitHub, and Slack settings under **Setup → Verification** in the desktop app |
| Cannot write to GitHub | Check the member's PAT scopes (`repo` + `project`) or the GitHub App permissions. `guildbotics member context --person <person_id> --check-credentials` also reports this |
| Slack events are not received | Check Socket Mode, the App-Level Token, and the bot events, and whether the service was started with **Event triggers** included (from the CLI, whether it was started with `--only scheduler`) |
| A command execution failed | Open the session on the **Diagnostics** screen in the desktop app and read the logs. You can also ask the AI assistant to investigate the cause |
| The scheduler stopped | The worker stops when **Stop after consecutive failures** (default: 3) is reached. Check the failure on the **Diagnostics** screen before restarting |

**Diagnostics logs**: a searchable execution summary is recorded in `<workspace>/.guildbotics/data/run/diagnostics.jsonl`, and the full events, logs, spans, and inputs/outputs are stored per execution as JSONL under `run/sessions/`. The **Diagnostics** screen in the desktop app shows both the execution history and the latest global / system session.

**Debug output**: environment variables for more verbose logging:

- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `AGNO_DEBUG`: extra debug output from the Agno engine (`true`/`false`)
- `GUILDBOTICS_TRANSCRIPT_DETAIL`: `standard` (default) or `full`. `standard` omits high-volume thinking/delta events and keeps only the last 8 KiB of AI CLI tool stderr
- `GUILDBOTICS_TRANSCRIPT_RETENTION_DAYS`: how many days session JSONL files are kept (default: `30`)
