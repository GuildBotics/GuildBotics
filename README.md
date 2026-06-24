<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

A multi-agent task scheduling and command execution framework.

GuildBotics enables you to:
- Manage multiple AI agents with different roles and personalities
- Schedule and execute commands (Markdown prompts, Python/Shell scripts, YAML workflows)
- Integrate with external services via pluggable adapters
- Support multiple LLM providers (Google Gemini, OpenAI, Anthropic Claude)

**Example use case**: The default workflow integrates with GitHub Projects to enable ticket-driven AI agent collaboration (see [GitHub Integration Example](#6-github-integration-example)).

---

## Important Notice (Disclaimer)

- This software is in alpha version. There is a very high possibility of breaking incompatible changes in the future, and malfunctions are expected to occur frequently, so use in production environments is not recommended.
- The author and distributor assume no responsibility for malfunctions of this software or damages caused thereby. In particular, due to malfunctions or runaway of AI agents, there is a possibility of fatal destruction to the system in use or external services, data loss, and leakage of confidential data. Use at your own risk and verify in an isolated test environment.

---

- [1. Key Features](#1-key-features)
  - [Core Framework](#core-framework)
  - [Built-in Capabilities](#built-in-capabilities)
- [2. Quick Start](#2-quick-start)
- [3. Environment](#3-environment)
  - [3.1. Supported Platforms](#31-supported-platforms)
  - [3.2. Required Software](#32-required-software)
  - [3.3. LLM API](#33-llm-api)
  - [3.4. CLI Agent](#34-cli-agent)
- [4. Installation](#4-installation)
- [5. Basic Usage](#5-basic-usage)
  - [5.1. Initial Setup](#51-initial-setup)
  - [5.2. Add Members](#52-add-members)
  - [5.3. Run Commands](#53-run-commands)
    - [5.3.1. Command Types and Placement](#531-command-types-and-placement)
    - [5.3.2. Command Execution Methods](#532-command-execution-methods)
  - [5.4. Schedule Features](#54-schedule-features)
    - [5.4.1. Routine Commands](#541-routine-commands)
    - [5.4.2. Scheduled Tasks](#542-scheduled-tasks)
    - [5.4.3. Cron Expression Format](#543-cron-expression-format)
    - [5.4.4. How Scheduling Works Internally](#544-how-scheduling-works-internally)
  - [5.5. Schedule Configuration Examples](#55-schedule-configuration-examples)
    - [Multi-Agent Scheduled Workflow](#multi-agent-scheduled-workflow)
    - [Multiple Schedule Patterns](#multiple-schedule-patterns)
    - [Randomization Usage](#randomization-usage)
  - [5.6. Slack Chat Workflow](#56-slack-chat-workflow)
    - [5.6.1. Prerequisites (Slack Side)](#561-prerequisites-slack-side)
      - [Basic Setup](#basic-setup)
      - [Adding Multiple Agents](#adding-multiple-agents)
    - [5.6.2. `person.yml` Example](#562-personyml-example)
- [6. GitHub Integration Example](#6-github-integration-example)
  - [6.1. Prerequisites](#61-prerequisites)
    - [6.1.1. Git Environment](#611-git-environment)
    - [6.1.2. Create a GitHub Project](#612-create-a-github-project)
    - [6.1.3. Prepare a GitHub Account for the AI Agent](#613-prepare-a-github-account-for-the-ai-agent)
      - [Using a Machine Account (Machine User)](#using-a-machine-account-machine-user)
      - [Using a GitHub App](#using-a-github-app)
      - [Using Your Own Account as a Proxy Agent](#using-your-own-account-as-a-proxy-agent)
  - [6.2. Setup for GitHub Integration](#62-setup-for-github-integration)
  - [6.3. Running the Ticket-Driven Workflow](#63-running-the-ticket-driven-workflow)
    - [6.3.1. Start](#631-start)
    - [6.3.2. How to Instruct the AI Agent](#632-how-to-instruct-the-ai-agent)
    - [6.3.3. Interacting with the AI Agent](#633-interacting-with-the-ai-agent)
  - [6.4. Capabilities](#64-capabilities)
- [7. Reference](#7-reference)
  - [7.1. Account-Related Environment Variables](#71-account-related-environment-variables)
  - [7.2. Configuration Files](#72-configuration-files)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Contributing](#9-contributing)

---

# 1. Key Features

## Core Framework
- **Multi-Agent Management**: Define multiple AI agents (persons) with distinct roles, personalities, and capabilities
- **Flexible Scheduling**: Cron-based scheduled commands and routine commands per person
- **Command Execution Framework**:
  - Markdown commands (LLM prompts with structured output)
  - Python scripts (with context injection)
  - Shell scripts
  - YAML workflows (command composition)
- **Brain Abstraction**: Swap LLM providers or delegate to CLI agents (Gemini CLI, Codex CLI, Claude Code, GitHub Copilot CLI)
- **Extensible Integrations**: Pluggable adapters for external services

## Built-in Capabilities
- **GitHub Integration** (default): Ticket management via GitHub Projects/Issues, PR creation, code hosting
- **Internationalization**: Multi-language support (English/Japanese)
- **Custom Commands**: Define reusable command templates per person/role

# 2. Quick Start

GuildBotics is configured through the **GuildBotics Desktop app (GUI)**, and run through the
**`guildbotics` CLI**. The desktop app bundles the CLI and installs a managed copy for CLI
agents on first launch. Setup writes plain config files (`.env` and `.guildbotics/config/...`),
so once configured you can run the CLI on any machine, including headless servers, by copying
those files over.

```bash
# 1. Configure with the GUI
#    Launch the GuildBotics Desktop app and complete project + member setup.
#    It writes .env and .guildbotics/config/... into your chosen workspace.
#    See desktop/README.md for installation.

# 2. Run with the managed CLI installed by the desktop app
#    If ~/.local/bin is on PATH, the "guildbotics" shim is available.
#    The stable absolute path always works:
~/.guildbotics/bin/guildbotics workspace status

# Run a custom command
echo "Hello" | ~/.guildbotics/bin/guildbotics run translate English Japanese

# Or start scheduler (runs default workflow: ticket_driven_workflow)
~/.guildbotics/bin/guildbotics start
```

See [Basic Usage](#5-basic-usage) for details, or [GitHub Integration Example](#6-github-integration-example) for the ticket-driven workflow setup.

# 3. Environment
## 3.1. Supported Platforms
GuildBotics runs on the following environments:
- OS: Linux (verified on Ubuntu 24.04) / macOS (verified on Sequoia)

## 3.2. Required Software
Please install the following software:
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## 3.3. LLM API
Please obtain one of the following API keys:
- Google Gemini API: [Google AI Studio](https://aistudio.google.com/app/apikey)
- OpenAI API: [OpenAI Platform](https://platform.openai.com/api-keys)
- Anthropic Claude API: [Anthropic Console](https://console.anthropic.com/settings/keys)

## 3.4. CLI Agent
Please install one of the following CLI agents and authenticate:

- [Gemini CLI](https://github.com/google-gemini/gemini-cli/)
- [OpenAI Codex CLI](https://github.com/openai/codex/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (requires Claude Pro or Max subscription)
- [GitHub Copilot CLI](https://docs.github.com/en/copilot/concepts/agents/about-copilot-cli)


# 4. Installation

Setup is performed with the **GuildBotics Desktop app**; command execution uses the
**`guildbotics` CLI**.

**Desktop app (setup + managed CLI):** Build or install the GuildBotics Desktop app. See
[desktop/README.md](desktop/README.md) for build and install instructions
(currently macOS Apple Silicon). On first launch, the app installs:

- `~/.guildbotics/bin/guildbotics`: managed GuildBotics CLI used by CLI agents and skills
- `~/.local/bin/guildbotics`: a small shim, only when the path is missing or already managed
- GuildBotics skill files for detected Codex, Claude Code, Gemini CLI, and GitHub Copilot CLI
  user skill directories. User-created or user-edited skills are not overwritten.

**Standalone CLI (headless / non-desktop environments):** Use `uv tool install guildbotics`
when you are not using the desktop app, or when you intentionally want a separately managed CLI
installation.

# 5. Basic Usage

## 5.1. Initial Setup

Project setup is done in the **GuildBotics Desktop app**.
When you launch the app, the **Project** settings open first. Configure:

- Language (English/Japanese)
- Workspace folder
- Project description
- Whether to enable GitHub integration

In GuildBotics, a **workspace** is the folder selected as the working location for a project.
GuildBotics writes configuration files such as the following into the workspace:

- `.env`: Environment variable settings
- `.guildbotics/config/team/project.yml`: Project definition
- `.guildbotics/config/intelligences/`: Brain and CLI agent settings

These are all plain text configuration files.
Because GuildBotics stores all settings in these files, you can copy the workspace folder to a
GUI-less environment such as a server and operate it with only the `guildbotics` CLI.

The selected workspace is recorded in `~/.guildbotics/data/active-workspace.json`.
Use the following commands to inspect or change the workspace from the CLI:

```bash
guildbotics workspace current
guildbotics workspace use /path/to/workspace
```

GuildBotics writes two kinds of local data:

- Computer-wide control files, such as the selected workspace and the CLI scheduler PID,
  are stored under `$HOME/.guildbotics/data`.
- Workspace-specific run data, such as member work directories, task and chat run records,
  diagnostics, prompt traces, and chat state, is stored under `<workspace>/.guildbotics/data` by
  default. To change this location, set `GUILDBOTICS_DATA_DIR` in `.env`.

## 5.2. Other Settings

After completing the project settings, configure the following initial settings:

- **LLM / CLI agent:** Default LLM, CLI agent, and LLM API keys
- **Members:** Team member creation and settings
- **GitHub:** GitHub integration settings (only when using GitHub)

## 5.3. Run Commands

### 5.3.1. Command Types and Placement

GuildBotics supports multiple types of commands. Commands should be placed in your project's configuration directory.

**Command Types**:

1. **Markdown commands** (`.md`): Executed as LLM prompts
   - Can specify model and output format in frontmatter
   - Ideal for text processing, translation, summarization, etc.

2. **Python scripts** (`.py`): Executed with context injection
   - Access to project and team member information
   - Ideal for complex processing and API integration

3. **Shell scripts** (`.sh`): Executed as shell commands
   - Ideal for system commands and tool invocation

4. **YAML workflows** (`.yml`): Execute multiple commands in composition
   - Ideal for command composition and conditional branching

**Command Placement**:

Commands can be placed in any of the following directories (in priority order):

1. **Built-in workflows**: Located in `guildbotics/templates/` within the package
   - Example: `workflows/ticket_driven_workflow`

2. **Per-member commands**: `.guildbotics/config/team/members/<person_id>/commands/`
   - Commands specific to a particular member

3. **Project-local commands**: `.guildbotics/config/commands/`
   - Commands shared across the project

4. **Global commands**: `~/.guildbotics/config/commands/`
   - Commands shared across all projects

**Simple example** (`~/.guildbotics/config/commands/translate.md`):
```markdown
If the following text is in ${1}, translate it to ${2}; if it is in ${2}, translate it to ${1}:
```

For detailed creation methods, see [Custom Command Development Guide](docs/custom_command_guide.en.md).

### 5.3.2. Command Execution Methods

**Manual execution**:

```bash
guildbotics run <command_name> [args...]
```

Example:
```bash
echo "Hello" | guildbotics run translate English Japanese
```

**Automated execution with scheduler**:

```bash
guildbotics start [routine_commands...]
```

By default, this starts:

- Task scheduler (routine commands / scheduled tasks)
- Event listener runner (event-driven receivers such as Slack Socket Mode)

If no command is specified, the scheduler runs `workflows/ticket_driven_workflow` as the default routine command.

You can also limit startup to one side:

```bash
guildbotics start --only scheduler
guildbotics start --only events
```

To stop the running scheduler:

```bash
guildbotics stop [--timeout <seconds>] [--force]
```

- Sends SIGTERM and waits up to `--timeout` seconds (default: 30).
- If it does not exit within the timeout and `--force` is specified, sends SIGKILL.

For an immediate force stop:

```bash
guildbotics kill
```

This is equivalent to `guildbotics stop --force --timeout 0`.

## 5.4. Schedule Features

GuildBotics allows you to configure scheduled tasks for each team member via the `person.yml` configuration file. The scheduler supports two types of command execution methods.

### 5.4.1. Routine Commands

**Routine Commands** (`routine_commands`) are commands that execute continuously in a round-robin fashion.

**Features**:
- Execute every minute when the scheduler is active
- If multiple commands are specified, they execute one at a time in order
- If not specified in `person.yml`, uses the default commands passed to `guildbotics start` (or `workflows/ticket_driven_workflow` if no arguments provided)

**Configuration example**:
```yaml
person_id: alice
name: Alice
is_active: true

# Override default routine commands (optional)
routine_commands:
  - workflows/ticket_driven_workflow
  - workflows/custom_workflow
```

**Typical use cases**:
- Periodic checking of task boards (e.g., `workflows/ticket_driven_workflow`)
- Continuous monitoring tasks
- Event-driven processing

### 5.4.2. Scheduled Tasks

**Scheduled Tasks** (`task_schedules`) are commands that execute at specific times defined by cron expressions.

**Features**:
- Checked every minute and executed when the current time matches the schedule
- Multiple schedule patterns can be configured for a single command
- Supports special randomization syntax (jitter)

**Configuration example**:
```yaml
person_id: alice
name: Alice
is_active: true

# Schedule commands to run at specific times
task_schedules:
  - command: workflows/cleanup
    schedules:
      - "0 2 * * *"        # Daily at 2:00 AM
      - "30 14 * * 5"      # Every Friday at 14:30
  - command: workflows/backup
    schedules:
      - "0 0 1 * *"        # First day of every month at midnight
```

**Typical use cases**:
- Periodic cleanup tasks
- Backups and report generation
- Tasks that need to run at scheduled times

### 5.4.3. Cron Expression Format

GuildBotics uses standard 5-field cron expressions:

```
* * * * *
│ │ │ │ │
│ │ │ │ └─── Day of week (0-6, Sunday=0)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

**Common Examples**:
```yaml
schedules:
  - "0 9 * * *"          # Every day at 9:00 AM
  - "*/15 * * * *"       # Every 15 minutes
  - "0 */2 * * *"        # Every 2 hours
  - "0 0 * * 0"          # Every Sunday at midnight
  - "30 8 1,15 * *"      # 1st and 15th of each month at 8:30 AM
  - "0 22 * * 1-5"       # Weekdays at 10:00 PM
```

**Special Randomization Syntax (Jitter)**:

GuildBotics extends standard cron with randomization syntax:

- `?`: Random value within the default range
- `?(min-max)`: Random value within the specified range

**Examples**:
```yaml
schedules:
  - "? 9 * * *"          # Random minute between 9:00-9:59 AM daily
  - "?(0-30) 14 * * *"   # Random minute between 14:00-14:30 daily
  - "0 ?(9-17) * * 1-5"  # Weekdays at random hour 9-17, on the hour
```

**Randomization use cases**:
- Avoiding simultaneous execution across multiple agents
- Simulating human-like irregular timing
- Load distribution across time windows

### 5.4.4. How Scheduling Works Internally

The scheduler behavior (from `guildbotics/drivers/task_scheduler.py` and `guildbotics/entities/task.py`):

**Architecture**:
1. **Per-person worker threads**: Each active team member gets a dedicated worker thread
2. **Minute-based check cycle**: Every minute, each worker thread:
   - Checks all `task_schedules` for the current person
   - Executes commands whose schedule matches the current time
   - Executes one `routine_command` in round-robin order

**Randomization handling**:
1. On initialization, calculates the next execution time for randomized schedules
2. For `?` fields, samples a random value within the boundary
3. Re-samples after each execution boundary is reached

**Error handling**:
- Consecutive command failures (default: 3) stop the worker thread
- Runtime diagnostics are recorded under `<workspace>/.guildbotics/data/run/diagnostics.jsonl` by default

## 5.5. Schedule Configuration Examples

This section provides practical examples of schedule configurations.

### Multi-Agent Scheduled Workflow

**Scenario**: Two agents with different schedules

**Agent 1** (`.guildbotics/config/team/members/agent1/person.yml`):
```yaml
person_id: agent1
name: Agent One
is_active: true

# Periodically execute ticket-driven workflow
routine_commands:
  - workflows/ticket_driven_workflow

# Generate morning standup report on weekday mornings at 9 AM
task_schedules:
  - command: workflows/morning_standup
    schedules:
      - "0 9 * * 1-5"     # Weekday mornings at 9:00 AM
```

**Agent 2** (`.guildbotics/config/team/members/agent2/person.yml`):
```yaml
person_id: agent2
name: Agent Two
is_active: true

# Periodically execute code review checks
routine_commands:
  - workflows/code_review_check

# Weekly and monthly maintenance tasks
task_schedules:
  - command: workflows/cleanup_old_branches
    schedules:
      - "0 0 * * 0"       # Sunday midnight
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *"  # First of month, random minute in 10 AM hour
```

**Start both agents**:
```bash
guildbotics start
```

Both agents will run concurrently, each executing their routine commands continuously and their scheduled tasks at the specified times.

### Multiple Schedule Patterns

Example of configuring multiple schedules for a single command:

```yaml
person_id: maintenance_bot
name: Maintenance Bot
is_active: true

task_schedules:
  # Execute cleanup at 2 AM on weekdays and midnight on weekends
  - command: workflows/cleanup
    schedules:
      - "0 2 * * 1-5"     # Weekdays at 2:00 AM
      - "0 0 * * 0,6"     # Weekends at midnight

  # Execute backup at 3 AM daily and midnight on first of month
  - command: workflows/backup
    schedules:
      - "0 3 * * *"       # Daily at 3:00 AM
      - "0 0 1 * *"       # First of month at midnight (monthly backup)
```

### Randomization Usage

Randomization configuration to avoid conflicts between multiple agents:

```yaml
person_id: agent_alpha
name: Agent Alpha
is_active: true

task_schedules:
  # Execute checks at random time in 9 AM hour
  - command: workflows/morning_check
    schedules:
      - "?(0-59) 9 * * 1-5"  # Weekdays, random minute between 9:00-9:59

  # Execute monitoring randomly during daytime hours
  - command: workflows/health_check
    schedules:
      - "0 ?(9-17) * * *"    # Daily, random hour between 9-17, on the hour
```

## 5.6. Slack Chat Workflow

In the Slack chat workflow, channels configured in `message_channels` of `person.yml` are monitored, and incoming events are delegated to the configured CLI agent. The CLI agent decides whether to reply, add a reaction, record a no-op, ask a question, or report a blocked state. Slack posts, replies, and reactions are written only through the public member capability commands under `guildbotics member chat ...`.

Scheduled command output posting remains separate: use `task_schedules` + `workflows/chat_post_command` for scheduled posts.

Incoming chat handling is performed by the event listener runner started with `guildbotics start`. If you start only the scheduler with `--only scheduler`, incoming chat events are not received.

For CLI-agent chat handling, GuildBotics runs `functions/handle_chat_event` from the per-agent work directory. By default, that directory is `<workspace>/.guildbotics/data/workspaces/<person_id>/`, where cloned repositories can be inspected. The workflow verifies completion through evidence recorded by `guildbotics member chat complete`; natural-language agent stdout alone is not treated as proof that Slack was updated.
You can define interests, preferences, and conversation participation rules in `character` within `person.yml`. Chat decisions and reply generation use this profile through the CLI agent.

### 5.6.1. Prerequisites (Slack Side)

#### Basic Setup

Create a Slack App that acts as the AI agent (send + receive).

1. Create a Slack App at https://api.slack.com/apps
2. Grant required scopes
   - Add them from `OAuth & Permissions` -> `Scopes`
   - Minimum required scopes (add based on conversation types you use)
     - `chat:write` (for `chat.postMessage`)
     - `reactions:write` (for `reactions.add`)
     - `channels:history` (for public channel `conversations.history`)
     - `groups:history` (for private channel `conversations.history`)
     - `im:history` (if handling DMs)
     - `mpim:history` (if handling group DMs)
   - If you want to configure via `channel_name`, also add name resolution scopes (`conversations.list`)
     - `channels:read` (public channels)
     - `groups:read` (private channels)
   - Reference URLs (official Slack docs)
     - `conversations.history`: `https://api.slack.com/methods/conversations.history`
     - `conversations.list`: `https://api.slack.com/methods/conversations.list`
     - `chat.postMessage`: `https://api.slack.com/methods/chat.postMessage`
     - `reactions.add`: `https://api.slack.com/methods/reactions.add`
3. Install the app to your workspace (reinstall may be required after scope changes)
4. Set Bot Token (`xoxb-...`) in environment variable `{PERSON_ID}_SLACK_BOT_TOKEN`
   - Example: for `alice`, set `ALICE_SLACK_BOT_TOKEN`
5. Configure Socket Mode
   - Enable `Enable Socket Mode` in `Socket Mode`
   - Enable `Event Subscriptions` and add bot events
     - For channels: `message.channels`, `message.groups`
     - For DMs: `message.im`, `message.mpim`
   - Issue an App-Level Token (`xapp-...`) in `Basic Information` and set `{PERSON_ID}_SLACK_APP_TOKEN`
     - Example: for `alice`, set `ALICE_SLACK_APP_TOKEN`
6. Invite the bot to target channels
7. Configure target channels in `person.yml` under `message_channels`

#### Adding Multiple Agents

You can add additional agents with the same setup. Alternatively, you can skip Socket Mode setup for later agents and share the communication path configured for the first AI agent.

To share incoming connections, set the later person's `{PERSON_ID}_SLACK_APP_TOKEN` to the same App-Level Token as the existing person.

- Example: if `alice` and `bob` share the same incoming connection
  - `ALICE_SLACK_APP_TOKEN=<alice_xapp_token>`
  - `BOB_SLACK_APP_TOKEN=<alice_xapp_token>`

If you want a separate incoming path (for example, separate workspaces or separate Slack Apps), create and configure another Slack App with its own Socket Mode / Event Subscriptions / App-Level Token.

### 5.6.2. `person.yml` Example

Configure chat receiving channels (`message_channels`) and scheduled posting (`task_schedules`) in `team/members/<person_id>/person.yml`.

```yaml
# team/members/alice/person.yml
person_id: alice
name: Alice
is_active: true

message_channels:
  - service: slack
    name: dev-chat
    chat:
      enabled: true
      participation: strict
      startup_backfill_minutes: 60
      backfill_interval_seconds: 300

task_schedules:
  - command: 'workflows/chat_post_command service=slack channel_id=C0123456789 command="examples/reports/ai_news_digest query=\"OpenAI OR Anthropic OR Gemini\" language=ja country=JP limit=10 max_age_hours=24"'
    schedules:
      - "0 9 * * 1-5"
```

Points:

- Monitored channels are defined in `message_channels`; entries with `chat.enabled: true` are monitored.
- `chat.participation` controls when the member joins a Slack thread: `strict` (default) handles direct mentions and follow-ups after the member was mentioned, `social` also allows unmentioned ambient participation for casual channels, and `muted` handles direct mentions only.
- On startup, GuildBotics backfills recent channel messages and known thread replies from Slack history. `startup_backfill_minutes` defaults to `60`; `backfill_interval_seconds` defaults to `300` and can be set to `0` to disable periodic history checks after startup.
- Incoming replies, reactions, no-op records, and completion evidence go through `guildbotics member chat reply|post|reaction add|noop|complete`.
- For scheduled posting, use `task_schedules` + `workflows/chat_post_command` (the post body is generated from a GuildBotics custom command output).
- Example: `examples/reports/ai_news_digest` gets candidate news from Google News RSS first, then an LLM formats it into a Slack-friendly summary.

Interactive member chat examples:

```bash
guildbotics member chat reply --person alice --service slack --channel-id C0123456789 --thread-ts 1777554000.000000 --body-file reply.md
guildbotics member chat reaction add --person alice --service slack --channel-id C0123456789 --message-ts 1777554000.000000 --reaction ack
```

Example scheduled command (AI news digest):

```bash
guildbotics run examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24
```

Example posting command (manual):

```bash
guildbotics run workflows/chat_post_command service=slack channel_name=dev-chat command='examples/reports/ai_news_digest query="OpenAI OR Anthropic OR Gemini" language=ja country=JP limit=10 max_age_hours=24'
```

This command first fetches candidate articles from Google News RSS, then uses an LLM to format a Japanese digest suitable for Slack.

# 6. GitHub Integration Example

This section describes how to use the default `ticket_driven_workflow` which integrates with GitHub Projects and Issues for ticket-based AI agent collaboration.

**Note**: This is one example use case. GuildBotics can be used for any scheduled automation tasks without GitHub integration.

## 6.1. Prerequisites

### 6.1.1. Git Environment
- Ticket-driven work is performed through the `guildbotics member ...` CLI. The workflow
  selects a GitHub Project item, starts the CLI agent in that member's work directory, and
  verifies that the agent recorded task completion. By default, the work directory is
  `<workspace>/.guildbotics/data/workspaces/<person_id>`. The agent itself performs
  clone/push/PR/comment/reply operations through
  `guildbotics member`.
- Configure each AI member's GitHub credentials in GuildBotics. GitHub/git writes use the
  assigned member's configured machine-user token or GitHub App installation, not the local
  `gh auth` user. Credential-required member commands load these values from
  the selected workspace `.env`, `GUILDBOTICS_ENV_FILE`, or `.env` in the current directory.
- For interactive CLI agent sessions, launch the GuildBotics Desktop app at least
  once after selecting the workspace. The app installs the GuildBotics skill and managed CLI
  under `~/.guildbotics/bin/guildbotics`. Configure the client to reject or require approval
  for `gh`, direct GitHub token/API writes, and `git push`. This is a guardrail against
  falling back to your own local GitHub account; it is not a complete technical sandbox
  against token exfiltration.
- When using Codex CLI as the CLI agent, verify its authentication and network reachability:
  ```bash
  codex doctor
  ```

### 6.1.2. Create a GitHub Project
Create a GitHub Projects (v2) project and add the following columns (statuses) in advance:
  - Todo
  - In Progress
  - Done

Note:
- For existing projects, you can map already-existing statuses to the above lanes with the settings described later.

### 6.1.3. Prepare a GitHub Account for the AI Agent
Prepare an account the AI agent will use to access GitHub. You can choose one of the following:

- **Machine Account** (Machine User)
  - Recommended if you want to keep the “work with an AI agent via the task board and Pull Requests” feel. However, per the [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements), free machine accounts are limited to one per user.
- **GitHub App**
  - There is no limit on the number of apps, but it cannot access GitHub Projects owned by a personal account. Also, GitHub UI clearly marks the app as a bot, which slightly changes the feel.
- Use your own account as a **proxy agent**
  - The simplest option. The visual impression is more like “talking to yourself” than interacting with a separate AI agent.

#### Using a Machine Account (Machine User)
After creating a machine account, do the following:

1. Add the machine account as a Collaborator to the project and repositories.
2. Issue a Classic PAT
  - Issue a **Classic** Personal Access Token.
  - Select the scopes `repo` and `project`.

#### Using a GitHub App
When creating a GitHub App, set the following permissions:

- Repository permissions
  - Contents: Read & Write
  - Issues: Read & Write
  - Projects: Read & Write
  - Pull requests: Read & Write
- Organization permissions
  - Projects: Read & Write

After creating the GitHub App, do the following:

1. On the app settings page, click “Generate a private key” to download a `.pem` file and save it.
2. Install the app to your repository/organization via “Install App” and record the installation ID. The last digits in the page URL (`.../settings/installations/<installation_id>`) are the installation ID; keep it for configuration.

#### Using Your Own Account as a Proxy Agent
If you use your own account as the AI agent, issue a **Classic** PAT. Select the scopes `repo` and `project`.


## 6.2. Setup for GitHub Integration

After completing [Basic Usage](#5-basic-usage) steps, verify the configuration from the
**Diagnostics / Verify** view in the Desktop app. This checks that each active member's GitHub,
LLM, and CLI agent settings are usable.

**Custom fields** are created automatically the first time GuildBotics operates on a GitHub
Project, so no explicit setup step is required. GuildBotics manages the `Agent` field to select
the AI agent when GitHub assignees are not enough.

**Lane mapping**: GuildBotics uses GitHub Projects statuses as lightweight workflow lanes.
By default it treats `Todo` as ready, `In Progress` as working, and `Done` as done.
The ready and done lanes also act as the boundaries of the work window: statuses positioned
**between** them on the board (for example `In Review`) are automatically treated as working
lanes, while statuses placed **before** ready (for example `Backlog`) or **at/after** done
(for example `Icebox`) are ignored. This means you can add intermediate or parked lanes by
ordering board columns alone, without touching `lane_map`.
If your GitHub Project uses custom status names for the ready/working/done lanes, map them with
the `services.ticket_manager.lane_map` key in `team/project.yml`
(see [Configuration Files](#72-configuration-files)). The desktop setup app also exposes these
lanes in the GitHub section: it offers your Project's status options when they can be read, and
otherwise falls back to manual entry. Defaults work for a standard `Todo` / `In Progress` / `Done`
board, so no lane configuration is required there.

## 6.3. Running the Ticket-Driven Workflow

### 6.3.1. Start
Start with:

```bash
guildbotics start
```

### 6.3.2. How to Instruct the AI Agent

To request a task from the AI agent, operate the GitHub Projects ticket as follows:

1. Create a ticket, select the target Git repository, and save it as an Issue
2. Describe instructions to the AI agent in the ticket
   - This becomes the prompt to the agent, so be as specific as possible
3. Assign the target AI agent, or set the `Agent` field when GitHub assignees are not enough
4. Move the ticket to the ready lane

Note:
The AI agent prepares repositories in the member work directory by running `guildbotics member git prepare` and works there. By default, that directory is `<workspace>/.guildbotics/data/workspaces/<person_id>`.

### 6.3.3. Interacting with the AI Agent
- If the AI agent has questions during work, it posts questions as ticket comments. Please respond in ticket comments. The agent periodically checks ticket comments and proceeds accordingly once answers are provided.
- When the AI agent completes a task, it posts the result, PR URL, review reply, or reaction itself through `guildbotics member ...`, then records `guildbotics member task complete`.
- For Pull Requests created from a ticket, write review results on the PR. GuildBotics checks unresolved review threads and delegates them back to the assigned agent.

## 6.4. Capabilities

With the ticket-driven workflow, you can:

- **Request tasks for AI agents on a task board**
  - Assign an AI agent to a ticket and move it to the ready lane to have the AI agent execute the task
- **Review AI agent results on the task board**
  - When the agent completes a task, it leaves a comment, PR, review reply, or reaction through the member capability
- **Create Pull Requests by AI agents**
  - When a task requires code changes, the agent publishes the member work-directory branch and creates or reuses a Pull Request through `guildbotics member github pr create`
- **Create tickets**
  - If you instruct the AI agent to create follow-up tickets, it creates real repository issues with `guildbotics member github issue create`

# 7. Reference

## 7.1. Account-Related Environment Variables

**LLM API Keys**:
- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**Slack Access**:
- `{PERSON_ID}_SLACK_BOT_TOKEN`: Slack Bot Token per person
- `{PERSON_ID}_SLACK_APP_TOKEN`: Slack App-Level Token per person

**GitHub Access** (per-person, format: `{PERSON_ID}_...`):
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: PAT for machine accounts/proxy agents
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: For GitHub Apps

If a `.env` file exists in the current directory, it is loaded automatically.
`guildbotics member` commands first honor `--workspace <dir>`. Without that option, they use the
selected workspace recorded by the desktop app or `guildbotics workspace use`, unless the command is
already running inside a configured workspace. The selected workspace sets `GUILDBOTICS_CONFIG_DIR`
to `<workspace>/.guildbotics/config` and, when `<workspace>/.env` exists, `GUILDBOTICS_ENV_FILE`.

Useful workspace commands:

```bash
guildbotics workspace status
guildbotics workspace current
guildbotics workspace use /path/to/workspace
guildbotics member --workspace /path/to/workspace context --person <person_id> --check-credentials
```

The fallback for non-desktop/headless use is `GUILDBOTICS_ENV_FILE` pointing to an absolute
`.env` path, or `.env` in the current directory. `guildbotics start` and the desktop runtime
set `GUILDBOTICS_ENV_FILE` automatically when they load the workspace `.env`.
`GUILDBOTICS_DATA_DIR` may be set in the workspace `.env` to move the directory used for
workspace-specific run data. If it is set in the process environment at startup and the workspace
`.env` does not define it, that process uses the startup value as its shared run-data directory.

## 7.2. Configuration Files

**Project Configuration** (`team/project.yml`):
- `name`: Project name
- `description`: Brief project description used as agent context
- `language`: Project language
- `repositories`: Repository definitions
- `services.ticket_manager`: GitHub Projects settings
- `services.ticket_manager.lane_map`: Maps ready, working, and done lanes to your GitHub Project's status names. Set this when your Project uses custom status names.
- `services.code_hosting_service`: GitHub repository settings

**Member Configuration** (`team/members/<person_id>/person.yml`):
- `person_id`: Unique identifier (lowercase alphanumeric, `-`, `_` only)
- `name`: Display name
- `is_active`: Whether the member acts as an AI agent
- `roles`: Role assignments
- `routine_commands`: Override default routine commands
- `task_schedules`: Cron-based scheduled commands
- `task_schedules[].command`: Scheduled posting can be configured with `workflows/chat_post_command ...`
- `message_channels`: Monitored channel settings (`chat.enabled`, `chat.event_source=socket_mode`, `channel_id`/`name`)

**Brain/CLI Agent Configuration**:
- `intelligences/cli_agent_mapping.yml`: Default CLI agent selection
- `intelligences/cli_agents/*.yml`: CLI agent scripts
- `team/members/<person_id>/intelligences/`: Per-agent overrides


# 8. Troubleshooting

**Diagnostics**: Runtime events and errors are recorded under `<workspace>/.guildbotics/data/run/diagnostics.jsonl` by default. You can also check them in the Desktop diagnostics view.

**Debug Output**: Set environment variables for detailed logging:
- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `LOG_OUTPUT_DIR`: Directory to write log files (e.g., `./tmp/logs`)
- `AGNO_DEBUG`: Extra debug output for the Agno engine (`true`/`false`)

# 9. Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Coding style and conventions
- Local lint/typecheck/test commands aligned with CI
- Testing guidelines
- Documentation standards
- Security best practices
