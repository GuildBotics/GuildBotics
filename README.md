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

- **OS**: Linux (verified on Ubuntu 24.04), macOS (verified on Sequoia), or Windows 11
  - The desktop app targets macOS Apple Silicon (arm64), Linux x86_64, and Windows x86_64
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**: used to build GuildBotics and install the CLI
- **An LLM API key** (obtain one of the following in advance):
  - Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey)
  - OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys)
  - Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys)
- **OpenSSH** (only to share one workspace across several machines):
  - Every participating machine needs an OpenSSH **client**. Windows 10 1809 and later include one, so no extra install is needed
  - The machine acting as the hub also needs an OpenSSH **server**: on Windows enable the "OpenSSH Server" optional feature, on macOS turn on Remote Login, on Linux install `openssh-server`
- **An AI CLI tool** (install one of the following in advance, launch it once, and complete authentication):
  - [Antigravity CLI](https://github.com/google-antigravity/antigravity-cli)
  - [OpenAI Codex CLI](https://github.com/openai/codex/)
  - [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (requires a Claude Pro or Max subscription)
  - [Grok Build](https://docs.x.ai/build/overview)
  - [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli)

With Codex, Claude Code, Grok Build, GitHub Copilot CLI, or Antigravity CLI, a member can carry a session over and resume where it left off. For authentication, how sessions are bound to Slack threads and tickets, and how execution permissions are configured, see [Native Agent Runtime](docs/native_agent_runtime.en.md).

### Installation

There is no general-purpose installer for the desktop app yet. Clone the repository and build it locally. In addition to uv, the build requires **Node.js 24 or later** and **Rust (rustup) stable 1.88 or later** (plus the WebKitGTK 4.1 development package on Linux). For macOS/Linux prerequisites and build steps, see [desktop/README.md](desktop/README.md). For the Windows native setup, NSIS build, and smoke checklist, see [Building GuildBotics Desktop on Windows](docs/windows_desktop_build.en.md).

On first launch, the desktop app installs the following:

- `~/.guildbotics/bin/guildbotics` on macOS/Linux, or `%USERPROFILE%\.guildbotics\bin\guildbotics.exe` on Windows: the managed GuildBotics CLI used by AI CLI tools and skills
- `~/.local/bin/guildbotics` on macOS/Linux: a small shim that forwards to the CLI above. On Windows the NSIS installer instead adds the managed bin directory to the user PATH and removes only its own entry during uninstall
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

- `.guildbotics/local/debug.env`: device-local, non-secret debug settings (log level)
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

The skill lives in the user skill directory each tool reads (`~/.claude/skills` for Claude Code, `~/.agents/skills` for Codex, and so on), so the same skill is used whether you start the tool from its CLI or from its app. To check that it is installed, see each tool's skill status under **Setup → LLM / AI CLI tools** in the desktop app.

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

**Prerequisite**: GitHub integration is operated only on repositories and Projects owned by an **organization**. A member's credential is either a fine-grained PAT scoped to the repositories that member works on, or a GitHub App, so that the per-repository boundary is enforced by GitHub's own authorization. Repositories and Projects owned by a personal account are out of scope (a personal-account Projects v2 board cannot be operated by a fine-grained PAT or by a GitHub App).

### What It Does

- **Assign work on the task board**: pick the member in the ticket's `Agent` field and move the ticket to the ready lane, and the member runs that task
- **Check the results**: when a task completes, the member leaves the result as a comment, a PR, a review reply, or a reaction
- **Create pull requests**: when code changes are needed, the member publishes a working branch and creates or reuses a pull request
- **Create tickets**: when asked to file a follow-up ticket, the member creates a real issue in the repository

### Create a GitHub Project

Create a GitHub Projects (v2) project in the organization and add the following columns (statuses) up front:

- Todo (ready)
- In Progress
- Done

If you want to keep the status names of an existing project, map them with the lane mapping described below.

### Prepare a GitHub Account for the AI Agent

Prepare an account the member uses to access GitHub. Any of the following works:

- **Machine account** (machine user)
  - Recommended if you want the feel of "working with an AI agent through the task board and pull requests". Note that under [GitHub's Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements), each user may create only one free machine account.
- **GitHub App**
  - There is no limit on the number of accounts. GitHub labels it as a bot, which takes away some of the atmosphere.

**Using a machine account**:

Issue a **fine-grained personal access token** for the machine account, scoped to the target repositories only. Classic PATs are not used: a classic PAT reaches every repository the account can see, so it cannot carry a per-repository boundary.

1. Make the machine account a **member** of the organization. Invitations are sent from the organization's People page (`https://github.com/orgs/<org>/people`) with **Invite member**. After it joins, confirm the account appears in the **Members** list on that page (if it appears under **Outside collaborators** instead, it is not a member, and an outside collaborator cannot select the organization as the fine-grained PAT's **Resource owner** in a later step)
2. Allow fine-grained PAT access on the organization side: in the organization's **Settings → Third-party Access → Personal access tokens → Settings**, select **Allow access via fine-grained personal access tokens**. If **Require administrator approval** is enabled, the token you create in a later step does not work until an organization owner approves it under **Settings → Third-party Access → Personal access tokens → Pending requests**
3. On each repository that member works on, open **Settings → Collaborators and teams** and add the machine account as a collaborator (Write or higher) (when the organization grants Write or higher to all members under **Member privileges → Base permissions**, no individual entry is needed; the default is Read)
4. Open the Project board, choose **Settings** from the **…** menu at the top right, and check **Manage access** (this is the Project's own settings page, not the organization's Settings). The **Who has access** section at the top of the page shows the Project's **Base role** — the permission every organization member gets on this Project (a separate setting from the repository Base permissions in step 3). When the Base role is Write or higher, no individual entry is needed. When it is Read or lower, add the machine account as a collaborator (Write or higher) with **Invite collaborators**
5. Sign in as the machine account, open **Settings → Developer settings → Personal access tokens → Fine-grained tokens**, and press **Generate new token**
6. Configure the token as follows

   - **Token name**: anything (for example `guildbotics-alice`)
   - **Expiration**: your choice (when it expires, reissue the token and re-register it in the desktop app)
   - **Resource owner**: select the target **organization**, not the machine account itself
   - **Repository access**: choose **Only select repositories** and select only the repositories that member works on
   - **Repository permissions**:
     - **Contents**: Read and write (cloning the repository and pushing the working branch)
     - **Issues**: Read and write (reading and writing issues, comments, labels, open/close, reactions)
     - **Metadata**: Read-only (added automatically once any other permission is selected)
     - **Pull requests**: Read and write (creating and updating PRs, review comments and replies, reactions)
     - **Workflows**: Read and write (only if the member changes files under `.github/workflows`)
   - **Organization permissions**:
     - **Projects**: Read and write (reading the Projects v2 board and creating / updating the `Agent` field)

7. Press **Generate token** and copy the token shown (it cannot be displayed again once you leave the page). If a **Pending** badge appears next to the token name, it is awaiting approval and does not work until an organization owner approves it on the **Pending requests** page from step 2; complete the approval before moving on
8. In the desktop app, open **Setup → Members → GitHub**, select "Machine Account (Machine User)", paste the token into **Access token**, and save

**Using a GitHub App**:

The member edit screen in the desktop app (select "GitHub Apps" on the GitHub tab) offers a "Register a new app" mode, which creates and installs the app on GitHub semi-automatically. You only approve the creation and pick the repositories in the browser; the App ID, private key, and installation ID are then filled in automatically, so the manual steps below are not needed.

When creating the GitHub App manually, configure the following permissions.

- **Repository permissions**: **Contents** / **Issues** / **Projects** / **Pull requests** / **Workflows** as Read & Write
- **Organization permissions**: **Projects** as Read & Write

When adding **Workflows** to an existing App, approve the requested permission update for each installation before using the App again.

After creating the GitHub App:

1. Use "Generate a private key" on the GitHub App settings page to download and store the `.pem` file
2. Install the app to the organization from "Install App" and obtain the **installation ID**. Select only the repositories that member works on. The trailing number of the URL shown after installation (`.../settings/installations/<installation ID>`) is the installation ID. Keep it — you need it during configuration

### Prepare Credentials and the Execution Environment

- Register each member's GitHub credentials (the PAT, or the GitHub App settings) in GuildBotics from member settings in the desktop app. Writes to GitHub / git use the assigned member's credentials, not your local `gh auth` user
- Ticket-driven work happens in a per-member working directory (default: `<workspace>/.guildbotics/local/clones/<person_id>`). Cloning, pushing, PR creation, and comments are executed by the member itself through the `guildbotics member` CLI
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

- **OS keychain:** secret values live in the OS secret store (macOS Keychain, Windows Credential Manager, Linux Secret Service). The workspace only keeps a non-secret index, `.guildbotics/config/secrets.yml`, listing key names and generations. Device-local generations are in `.guildbotics/local/secrets.json`. There is no `.env` secret backend.
- **Windows credentials:** GuildBotics stores secret values as UTF-8 Credential Manager blobs, allowing ASCII-heavy PEM private keys to use the full 2,560-byte Windows limit. An import validates every value before writing any of them.
- **Precedence:** real environment variables > OS keychain. GuildBotics does not read a workspace `.env`.
- **GitHub App private key:** member save absorbs the PEM into the keychain. Generated registration files are written under the OS temporary directory and deleted after absorb. The key material is never exposed in environment variables.
- **Exchange format:** `guildbotics secrets export` / `import` use dotenv only as a transfer file. `secrets set --from-file` absorbs a file (for example a PEM) into the keychain.

Manage secrets with the `guildbotics secrets` CLI (see the [CLI Reference](docs/cli_reference.md#guildbotics-secrets) for all subcommands and options):

```bash
guildbotics secrets status                        # OS secret-store availability and key count
guildbotics secrets pull                          # fetch missing or outdated values from the hub
guildbotics secrets push                          # give the hub the values entered here
guildbotics secrets export --file secrets.env     # export secrets for a move
guildbotics secrets import secrets.env            # import them on the new machine
```

When a workspace is shared between machines, values never enter the shared history: they
move through the hub machine's OS secret store, and only when you send or fetch them. See
[Share credentials with each machine](#share-credentials-with-each-machine).

Secrets are stored per workspace (keychain entries are namespaced by the `store_id` in `secrets.yml`). Choose the target workspace with `--workspace` before the subcommand. Without it, the active workspace is required. `guildbotics secrets status` always shows where the target resolved on its `workspace:` line.

```bash
guildbotics secrets --workspace /path/to/workspace status
guildbotics secrets --workspace /path/to/workspace list
```

### Run on a Server

All non-secret configuration is stored as plain text files inside the workspace, so once you have set things up you can move them to an environment without a GUI (a headless server, for example) and operate with the CLI alone.

1. Install the CLI on the target machine with `uv tool install guildbotics`
2. Copy the workspace folder to the target machine
3. Move the secrets: run `guildbotics secrets export --file ...` on the source and `guildbotics secrets import ...` on the target (delete the export file afterwards). Keychain entries themselves never leave the machine
4. On servers without a keychain, set up an OS secret store or pass the secrets as environment variables at run time

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

When a workspace is synchronized, the service also has one persistent owner on
the Hub. `guildbotics start` and the desktop service screen must reach the Hub
and confirm this machine as the owner before accepting service work. A different
owner blocks new service work; an owner change is detected by the common
execution boundary and stops the old service. A Hub outage does not cancel work
already in progress, but it does prevent new service work until ownership can be
checked again.

Ownership is not a lease and is never changed automatically. After confirming
that the old machine's service has stopped, use **Take over on this machine** in
the desktop Sync settings. The live work indicator is transient relay state,
not Git history, and disappears after its publisher stops heartbeats and the
snapshot expires. Completed
work is kept in shared TaskRun records so a service restart does not accept the
same input again after the result has been pushed.

### Account-Related Environment Variables

**LLM API keys**:

- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack access** (per member, format: `{PERSON_ID}_...`):

- `{PERSON_ID}_SLACK_BOT_TOKEN`: Slack Bot Token
- `{PERSON_ID}_SLACK_APP_TOKEN`: Slack App-Level Token

**GitHub access** (per member, format: `{PERSON_ID}_...`):

- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: fine-grained PAT for a machine account
- GitHub App IDs live in the member YAML (`account_info.github_app_id` / `github_installation_id`); the PEM is stored in the OS keychain as `{PERSON_ID}_GITHUB_PRIVATE_KEY`

Secrets stored in the OS keychain are loaded automatically. GuildBotics does not read a workspace `.env`.

### Workspace and Data Locations

The workspace in use is recorded in `~/.guildbotics/data/active-workspace.json`. From the CLI, inspect and change it with:

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
guildbotics workspace migrate --from /path/to/source-checkout --to /path/to/guildbotics-workspace
```

The workspace used by `guildbotics member` commands is resolved in this order:

1. `--workspace <dir>` given before the subcommand
2. An explicit `GUILDBOTICS_WORKSPACE_ROOT` or `GUILDBOTICS_CONFIG_DIR` that points at `<workspace>/.guildbotics/config`
3. The workspace in use as recorded by the desktop app or `guildbotics workspace use`

The process cwd is never treated as a workspace. From the selected workspace, `GUILDBOTICS_CONFIG_DIR` is set to `<workspace>/.guildbotics/config`.

GuildBotics stores three kinds of local data:

- Machine-wide management information — the workspace in use, the CLI scheduler PID, and so on — is stored in `$HOME/.guildbotics/data`
- Shared workspace state — memory, chat control, task-run evidence, activity events — is stored in `<workspace>/.guildbotics/state`
- Device-local data — diagnostics, transcripts, chat cache, member clones, AI CLI sessions — is stored in `<workspace>/.guildbotics/local`

### Share a Workspace Across Machines

One workspace can be shared by several of your machines. Settings, members, memory, chat
state, and activity history are kept in step; API keys, member clones, diagnostics, and
hotkeys stay on the machine they were made on.

Sharing runs through a **hub**: one machine you choose, holding a Git repository the others
push to and fetch from over SSH. It is not a service and it is not hosted by anyone else —
it is a directory under `~/.guildbotics/hub` on a machine you already own.

The Hub also holds the current service owner and transient live snapshots. The
owner is persistent and changes only through an explicit takeover after you have
confirmed that the previous service stopped. If the Hub is unavailable, an
already-running service keeps its current work but cannot start new service work
until the owner can be checked. Live snapshots are not committed to Git; when a
publisher exits its heartbeat stops, and the Hub removes that snapshot after it
expires.

Everything below is done in the desktop app's **Settings**. Hosting the hub and this
device's SSH key are under **Device and hub** (they belong to the machine, not to a
workspace); connecting a workspace is under **Sync**.

#### Set up the first machine

1. On the machine that will be the hub, select **Host the hub on this machine** under
   **Device and hub**. Make sure its OpenSSH server is running (see
   [What You Need](#what-you-need)). This creates `~/.guildbotics/hub/` and its `hub.json`;
   the repository for a workspace is created later, when the first machine registers one.
2. On the machine holding the workspace, select **Create a key** under **Device and hub →
   This device's SSH key**, then copy the key it shows. It is `~/.ssh/id_ed25519`, and an
   existing key of that name is used as it is rather than replaced.
3. Add that public key to the hub machine's `~/.ssh/authorized_keys`. On the hub machine:

   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\n' 'ssh-ed25519 AAAA... guildbotics alice@mac-studio' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
   ```

   GuildBotics cannot do this for you: that step is what proves you already have access to
   the machine. It is a prerequisite rather than a fallback — the next step asks the hub for
   its workspace list over SSH, and synchronization runs SSH non-interactively, so an
   unregistered key fails there instead of prompting for a password.
4. Check that the hub machine answers its own command over SSH, from the machine holding the
   workspace:

   ```bash
   ssh user@hub-host guildbotics hub status
   ```

   GuildBotics reaches the hub by running `guildbotics hub …` there, so the command has to be
   on the PATH that non-interactive SSH sessions get — which is not always the one an
   interactive login has. If it is not found, see
   [`guildbotics` is not found over SSH](#guildbotics-is-not-found-over-ssh).
5. Back in the desktop app, under **Sync**, enter the hub as `user@host` and select
   **Look up**.
6. Compare the fingerprint shown against the hub machine's own, then confirm it. On the hub
   machine, print it with:

   ```bash
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   This is the only point at which the key is checked: synchronization runs SSH
   non-interactively afterwards, so its usual first-contact prompt never appears.
7. Select **Register this workspace on the hub**. The hub's repository for this workspace,
   `~/.guildbotics/hub/workspaces/<workspace id>/repository.git`, is created now.

##### `guildbotics` is not found over SSH

A non-interactive SSH session does not read the shell startup files an interactive login
does, so a `guildbotics` installed under `~/.local/bin` is often invisible to it. The
shortest fix is a link from a directory that is always on the PATH, made on the hub machine:

```bash
sudo ln -sf ~/.guildbotics/bin/guildbotics /usr/local/bin/guildbotics
```

`~/.guildbotics/bin/guildbotics` is the managed shim, so it keeps working across
reinstalls of the CLI itself. On Windows the installer puts the managed bin on the user
PATH, which SSH sessions also get.

#### Add another machine

On the new machine, create its SSH key and register it with the hub as above, then either:

- **Take a copy**: choose the workspace from the hub's list to create a new workspace
  directory from it, or
- **Join with what you already have**: on a machine that already has a workspace, look up
  the hub and select **Join…** next to the matching workspace.

Joining is not an overwrite. This machine's content is committed first; for any file both
sides hold, the hub's version wins; files only this machine has are sent to the hub. What
is shown before you confirm is exactly that division. A commit pushed aside this way is
kept locally — see [Recover a change that was not applied](#recover-a-change-that-was-not-applied).

#### Share credentials with each machine

The **values** of API keys and tokens are not shared. What travels between machines is a
key name and a generation; the value stays in each machine's own OS secret store. The hub
machine's secret store is the distribution point, and a value moves only when you send or
fetch it, over the standard input and output of one SSH command. It is never written to
the Git repository, to a relay file on the hub, or to a temporary file.

The list and the transfers are under **Settings → Sync → Credentials on this machine**.
The provider and member forms where a value is typed show its state and link here; they
carry no transfer buttons of their own.

**On a machine you have just added**, choose **Fetch all credentials** once. Everything
this machine lacks or holds an older value for arrives in a single exchange and goes
straight into this machine's OS secret store. Nothing is retyped.

**When you update a value**, choose **Send** on the machine where you typed it. The shared
files are taken first, so a machine that has been offline never sends from a generation the
others have moved past. Once the value reaches the hub the shared generation moves on, and
the other machines show *Updated elsewhere*; choose **Fetch** on each to catch up.

**Send all** covers every value the hub does not have yet, so connecting an existing
workspace to a hub — or rebuilding one — hands it everything in a single action.

If two machines change the same key, the one that receives the other's update shows *Changed
on two machines*. Choose which to keep on that key's own row: **Send** makes this machine's
value the new generation, **Fetch** takes the other machine's, and either way the other
machine is told about it. Nothing has to be typed again. The bulk actions leave such a key
alone, because which value to keep is a decision per key.

If the record of the shared generation is interrupted after the hub has already stored the
value, the key shows *Needs checking*. Choosing **Send** on any machine that holds a value
settles it: a send is built on the generation the hub reports, so it moves past the
interrupted one and the record is made. That works from a machine that knows nothing about
the interrupted send, and after the value has been entered again on the machine that was
sending.

**Fetch** is not offered for that key meanwhile — taking a generation the workspace has no
record of would remove the very thing saying it still needs settling.

On a machine that runs no desktop app, use the CLI:

```bash
guildbotics secrets status   # this machine's state, and which keys need attention
guildbotics secrets pull     # fetch everything missing or outdated, in one exchange
guildbotics secrets push     # give the hub the values entered on this machine
```

While an OS secret store is locked, nothing can be sent or fetched. GuildBotics never falls
back to a plaintext file, so unlock it and try again.

**To use a headless Linux machine as a device**, its secret store (Secret Service) has to
run without a desktop session.

1. Install a Secret Service implementation (`gnome-keyring` or similar) and D-Bus.
2. Enable lingering, so the user's systemd and session bus keep running while nobody is
   logged in.

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

   There is no need to keep `gnome-keyring-daemon` running yourself: recent distributions
   start it on demand through socket activation (`gnome-keyring-daemon.socket`). A daemon
   started that way holds the control socket and the D-Bus name **while still locked**,
   though, so the next step stops it before starting an unlocked one in its place.

3. After each restart, restart the daemon with an unlock. The keyring password is normally
   the account's login password (the one it was created with).

   ```bash
   ssh user@linux-host 'systemctl --user stop gnome-keyring-daemon.socket gnome-keyring-daemon.service 2>/dev/null; pkill -u "$USER" -x gnome-keyring-daemon; sleep 1; printf "%s" "<keyring password>" | gnome-keyring-daemon --daemonize --unlock'
   ```

   Run `pkill` with `-x` (exact process-name match) and nothing else: with `-f`, the
   pattern matches this command line itself and kills the shell running it.

4. The command's output does not say whether the unlock worked, so always check.

   ```bash
   ssh user@linux-host 'busctl --user get-property org.freedesktop.secrets /org/freedesktop/secrets/collection/login org.freedesktop.Secret.Collection Locked'
   ```

   `b false` means unlocked. If it stays `b true`, the password did not match: the keyring
   does not follow login password changes, so an account whose password was changed later
   still uses the old one.

   Until that unlock, workflows on that machine that need a secret fail at run time and
   no transfer can start. If the hub runs on that machine, fetches from every other machine
   stop for the same reason, and they show *The hub machine's secret store is locked*.

#### Revoke a machine

When a machine is lost, remove its public key from the hub.

1. Find that machine's SSH public key fingerprint in the device list under
   **Settings → Sync**.
2. On the hub machine, delete the line with that fingerprint from
   `~/.ssh/authorized_keys`.

   ```bash
   ssh-keygen -l -f ~/.ssh/authorized_keys
   ```

   GuildBotics does not edit `authorized_keys`. Deciding which line to remove, and removing
   it, stay with you.

3. Values already stored on the lost machine cannot be erased remotely. Revoke and reissue
   the tokens and API keys at GitHub, Slack, and your LLM providers. Enter each reissued
   value on a machine you still have, then use **Send** to give it to the others.

#### Rebuild the hub

Any participating machine holds a full copy, so a lost hub is rebuilt from one of them.

1. Choose a machine to host the new hub and select **Host the hub on this machine**.
2. Add every other machine's public key to the new hub machine's `~/.ssh/authorized_keys`,
   and check `guildbotics` answers over SSH there, as in
   [Set up the first machine](#set-up-the-first-machine). A new hub machine knows nothing
   about the keys the old one held.
3. On each other machine, open **Connected hub → Connect to a different hub** and connect to
   the new address.

Content only one machine has is sent to the new hub rather than discarded, so the order the
machines reconnect in does not lose anything.

#### When two machines change the same file

Whichever change reaches the hub first is the one that is kept. The other machine's commit
is not merged and not discarded: it is set aside locally, and it appears on that machine
under **Settings → Sync**, in "Changes set aside", with the time, the files, and a recovery
ID (the warning band's "Sync settings" button opens the same screen). Activity History keeps
the same record. Synchronization does not stop, and no one is asked to resolve anything.

Content that was not adopted when a machine with an existing `.guildbotics/` joined the hub
is listed there the same way.

#### Recover a change that was not applied

This is an exception procedure, not part of normal use. GuildBotics never restores a set
aside change automatically, and neither the desktop app nor the API will show its contents.
Reading it takes the Git commands below.

The set aside content exists **only on the machine that made the change**. Run the commands
on the machine whose **Settings → Sync** lists the change under "Changes set aside", and
fill in the placeholders as follows.

| Placeholder | What goes there |
| --- | --- |
| `<workspace>` | The workspace directory, as shown in the "Workspace" field under "Settings → Project" |
| `<recovery ID>` | The value in the "Recovery ID" column |
| `<path>` | One entry from the "Files" column (for example `config/team/members/yuki/person.yml`) |
| `<path>...` | The "Files" entries separated by spaces (for example `config/team/project.yml config/team/members/yuki/person.yml`) |

1. Read what was set aside. This shows the difference from what is in use now (`+` lines
   are the set aside side):

   ```bash
   git -C "<workspace>/.guildbotics" diff HEAD "refs/guildbotics/rejected/<recovery ID>" -- <path>...
   ```

   To print the set aside side in full rather than as a diff, name one file at a time:

   ```bash
   git -C "<workspace>/.guildbotics" show "refs/guildbotics/rejected/<recovery ID>:<path>"
   ```

   A file the set aside change deleted has no content to show, so `git diff` reports only the
   deletion. Binary files such as images do not display usefully; export them in the next
   step instead.

2. If you need the files outside Git, export them together as a zip. Point
   `<output-directory>` outside `<workspace>/.guildbotics/`; `<output-name>` is up to you.
   A zip is used because saving through shell redirection (`>`) can change the text
   encoding or corrupt binary files, depending on the shell.

   ```bash
   git -C "<workspace>/.guildbotics" archive --format=zip --output="<output-directory>/<output-name>.zip" "refs/guildbotics/rejected/<recovery ID>" -- <path>...
   ```

3. Make the changes you still want again through GuildBotics, starting from the content
   currently in use. They are shared like any other edit.

Do not do any of the following to the synchronization repository, as they replace shared
content without going through the rules above:

- `checkout`, `switch`, `reset`, `merge`, `rebase`, `cherry-pick`, or `push` a
  `refs/guildbotics/rejected/...` ref
- Write the export into `<workspace>/.guildbotics/`

A set aside change cannot be recovered if the machine that made it is lost, or if it has
been discarded there. Nothing prunes them automatically.

Once you are done looking, discard it from **Settings → Sync**, under "Changes set
aside". Discarding removes the set aside content and clears the warning. This machine
holds the only copy, so look at the content with the steps above before
discarding; the activity history record stays either way.

#### What the sidebar is telling you

| State | Meaning | What to do |
| --- | --- | --- |
| In sync | This machine and the hub hold the same content | Nothing |
| Waiting to send | Local changes have not reached the hub yet | Nothing; they are sent automatically |
| Receiving | Content from the hub is being taken in | Wait |
| Hub unreachable | Local work continues; sharing is delayed | Wait, or select **Try again**. If it never recovers, check the SSH prerequisites in [Set up the first machine](#set-up-the-first-machine) |
| Changes that cannot be sent | Some files cannot be shared until repaired here | Open **Sync** for the list and the reason |
| Shared data problem | Content could not be reconciled automatically | Open **Sync** |
| Update required | Another machine wrote something a newer version produced | Update GuildBotics on this machine |

### Configuration Files

**Project settings** (`team/project.yml`):

- `name`: project name
- `description`: short project description used as agent context
- `language`: project language
- `services.ticket_manager`: GitHub Projects settings
- `services.ticket_manager.lane_map`: maps the ready / working / done lanes to GitHub Project status names. Set this when your Project uses its own status names
- `services.code_hosting_service`: code hosting service settings (the GitHub owner used for repository operations)

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
| `guildbotics` command not found | On macOS/Linux, run `~/.guildbotics/bin/guildbotics` and check `~/.local/bin` in PATH. On Windows, open a new shell after installation and check `%USERPROFILE%\.guildbotics\bin` in the user PATH |
| Not sure which workspace is in use | Check and change it under **Setup → Project** in the desktop app. From the CLI, use `guildbotics workspace status` / `guildbotics workspace use <path>` |
| A member does not work, or the configuration looks wrong | Validate the LLM, AI CLI tool, GitHub, and Slack settings under **Setup → Verification** in the desktop app |
| Cannot write to GitHub | Check the fine-grained PAT permissions (repository Contents / Issues / Pull requests as Read and write, organization Projects as Read and write), that the target repository is listed under **Only select repositories**, and that the organization allows and has approved the token. For a GitHub App, check its permissions. `guildbotics member context --person <person_id> --check-credentials` also reports this |
| Slack events are not received | Check Socket Mode, the App-Level Token, and the bot events, and whether the service was started with **Event triggers** included (from the CLI, whether it was started with `--only scheduler`) |
| A command execution failed | Open the session on the **Diagnostics** screen in the desktop app and read the logs. You can also ask the AI assistant to investigate the cause |
| The scheduler stopped | The worker stops when **Stop after consecutive failures** (default: 3) is reached. Check the failure on the **Diagnostics** screen before restarting |

**Diagnostics logs**: a searchable execution summary is recorded in `<workspace>/.guildbotics/local/run/diagnostics.jsonl`, and the full events, logs, spans, and inputs/outputs are stored per execution as JSONL under `run/sessions/`. The **Diagnostics** screen in the desktop app shows both the execution history and the latest global / system session.

**Debug output**: environment variables for more verbose logging:

- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `AGNO_DEBUG`: extra debug output from the Agno engine (`true`/`false`)

Transcript detail (`standard` / `full`) and retention days are configured from the desktop app's **Diagnostics** screen and stored in `.guildbotics/config/transcripts.yml`.
