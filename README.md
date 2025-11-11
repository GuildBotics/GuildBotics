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
  - [Supported Integrations](#supported-integrations)
- [4. Installation](#4-installation)
- [5. Basic Usage](#5-basic-usage)
  - [5.1. Initial Setup](#51-initial-setup)
  - [5.2. Add Members](#52-add-members)
  - [5.3. Run Commands](#53-run-commands)
    - [Run a custom command](#run-a-custom-command)
    - [Start the scheduler](#start-the-scheduler)
  - [5.4. Schedule Configuration](#54-schedule-configuration)
    - [Configuration Structure in person.yml](#configuration-structure-in-personyml)
    - [Routine Commands vs Scheduled Tasks](#routine-commands-vs-scheduled-tasks)
    - [Cron Expression Format](#cron-expression-format)
    - [Special Randomization Syntax](#special-randomization-syntax)
    - [Command Placement](#command-placement)
    - [How Scheduling Works Internally](#how-scheduling-works-internally)
    - [Example: Multi-Agent Scheduled Workflow](#example-multi-agent-scheduled-workflow)
- [6. GitHub Integration Example](#6-github-integration-example)
  - [6.1. Prerequisites](#61-prerequisites)
    - [6.1.1. Git Environment](#611-git-environment)
    - [6.1.2. Create a GitHub Project](#612-create-a-github-project)
    - [6.1.3. Prepare a GitHub Account for the AI Agent](#613-prepare-a-github-account-for-the-ai-agent)
      - [Using a Machine Account (Machine User)](#using-a-machine-account-machine-user)
      - [Using a GitHub App](#using-a-github-app)
      - [Using Your Own Account as a Proxy Agent](#using-your-own-account-as-a-proxy-agent)
    - [6.1.4. LLM API](#614-llm-api)
    - [6.1.5. CLI Agent (Optional)](#615-cli-agent-optional)
  - [6.2. Setup for GitHub Integration](#62-setup-for-github-integration)
  - [6.3. Running the Ticket-Driven Workflow](#63-running-the-ticket-driven-workflow)
    - [6.3.1. Start](#631-start)
    - [6.3.2. How to Instruct the AI Agent](#632-how-to-instruct-the-ai-agent)
    - [6.3.3. Interacting with the AI Agent](#633-interacting-with-the-ai-agent)
  - [6.4. Capabilities](#64-capabilities)
- [7. Reference](#7-reference)
  - [7.1. Account-Related Environment Variables](#71-account-related-environment-variables)
  - [7.2. Configuration Files](#72-configuration-files)
  - [7.3. Custom Commands](#73-custom-commands)
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
- **Brain Abstraction**: Swap LLM providers or delegate to CLI agents (Gemini CLI, Codex CLI, Claude Code)
- **Extensible Integrations**: Pluggable adapters for external services

## Built-in Capabilities
- **GitHub Integration** (default): Ticket management via GitHub Projects/Issues, PR creation, code hosting
- **Internationalization**: Multi-language support (English/Japanese)
- **Custom Commands**: Define reusable command templates per person/role

# 2. Quick Start

```bash
# Install
uv tool install guildbotics

# Initialize configuration
guildbotics config init

# Add an AI agent member
guildbotics config add

# Run a custom command
echo "Hello" | guildbotics run translate English Japanese

# Or start scheduler (runs default workflow: ticket_driven_workflow)
guildbotics start
```

See [Basic Usage](#5-basic-usage) for details, or [GitHub Integration Example](#6-github-integration-example) for the ticket-driven workflow setup.

# 3. Environment
- OS: Linux (verified on Ubuntu 24.04) / macOS (verified on Sequoia)
- Runtime: `uv` (automatically fetches/manages Python)

## Supported Integrations
- **LLM Providers**: Google Gemini, OpenAI, Anthropic Claude
- **CLI Agents**: Gemini CLI, OpenAI Codex CLI, Claude Code
- **GitHub Integration**: Projects (v2), Issues, Pull Requests

# 4. Installation

```bash
uv tool install guildbotics
```

# 5. Basic Usage

## 5.1. Initial Setup

Initialize configuration interactively:

```bash
guildbotics config init
```

This command will:
- Select language (English/Japanese)
- Choose configuration directory location
- Configure LLM API settings
- Set up basic project structure

The following files are created:
- `.env`: Environment variables
- `.guildbotics/config/team/project.yml`: Project definition
- `.guildbotics/config/intelligences/`: Brain and CLI agent settings

## 5.2. Add Members

Add AI agents or human team members:

```bash
guildbotics config add
```

This command prompts for:
- Member type (human, AI agent, etc.)
- Display name and person_id
- Roles (e.g., programmer, architect, product_owner)
- Speaking style (for AI agents)

Creates:
- `.guildbotics/config/team/members/<person_id>/person.yml`
- Environment variables in `.env` (for credentials)

Repeat for each team member.

## 5.3. Run Commands

### Run a custom command

```bash
guildbotics run <command_name> [args...]
```

Example:
```bash
echo "Hello" | guildbotics run translate English Japanese
```

See [Custom Commands](#73-custom-commands) and the [Custom Command Development Guide](docs/custom_command_guide.en.md) for creating your own commands.

### Start the scheduler

```bash
guildbotics start [routine_commands...]
```

Starts the task scheduler to execute routine commands and scheduled tasks. If no commands are specified, runs the default `workflows/ticket_driven_workflow`.

To stop:
```bash
guildbotics stop
```

## 5.4. Schedule Configuration

GuildBotics allows you to configure scheduled tasks for each team member via the `person.yml` configuration file. This enables AI agents to execute commands automatically at specified times.

### Configuration Structure in person.yml

Add schedule settings to your member's `person.yml` file (`.guildbotics/config/team/members/<person_id>/person.yml`):

```yaml
person_id: alice
name: Alice
is_active: true

# Override default routine commands (optional)
routine_commands:
  - workflows/ticket_driven_workflow
  - workflows/custom_workflow

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

### Routine Commands vs Scheduled Tasks

**Routine Commands** (`routine_commands`):
- Execute continuously in a round-robin fashion
- Run every minute when the scheduler is active
- If not specified in `person.yml`, uses the default commands passed to `guildbotics start`
- Example: `workflows/ticket_driven_workflow` checks for new tasks repeatedly

**Scheduled Tasks** (`task_schedules`):
- Execute at specific times defined by cron expressions
- Each task has a command and one or more schedule patterns
- Checked every minute; executed when the current time matches the schedule

### Cron Expression Format

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

### Special Randomization Syntax

GuildBotics extends standard cron with randomization syntax for jitter:

- `?`: Random value within the default range
- `?(min-max)`: Random value within the specified range

**Examples**:
```yaml
schedules:
  - "? 9 * * *"          # Random minute between 9:00-9:59 AM daily
  - "?(0-30) 14 * * *"   # Random minute between 14:00-14:30 daily
  - "0 ?(9-17) * * 1-5"  # Weekdays at random hour 9-17, on the hour
```

This is useful for:
- Avoiding simultaneous execution across multiple agents
- Simulating human-like irregular timing
- Load distribution across time windows

### Command Placement

Commands referenced in schedules can be:

1. **Built-in workflows**: Located in `guildbotics/templates/` within the package
   - Example: `workflows/ticket_driven_workflow`

2. **Custom commands**: Place in your configuration directory
   - Global: `~/.guildbotics/config/commands/`
   - Project-local: `.guildbotics/config/commands/`
   - Per-member: `.guildbotics/config/team/members/<person_id>/commands/`

3. **Command types**:
   - `.md` files: LLM prompts (Markdown commands)
   - `.py` files: Python scripts with context injection
   - `.sh` files: Shell scripts
   - `.yml` files: YAML workflow compositions

**Example custom scheduled command** (`~/.guildbotics/config/commands/workflows/daily_report.md`):
```markdown
---
model: gemini-2.0-flash-exp
---
Generate a daily report summarizing:
- Tasks completed yesterday
- Tasks in progress
- Blocked tasks requiring attention

Format the output as a markdown document.
```

### How Scheduling Works Internally

The scheduler behavior (from `guildbotics/drivers/task_scheduler.py`):

1. **Per-person worker threads**: Each active team member gets a dedicated worker thread
2. **Minute-based check cycle**: Every minute, the scheduler:
   - Checks all `task_schedules` for the current person
   - Executes commands whose schedule matches the current time
   - Executes one `routine_command` in round-robin order
3. **Randomization handling** (from `guildbotics/entities/task.py`):
   - On initialization, calculates the next execution time for randomized schedules
   - For `?` fields, samples a random value within the boundary
   - Re-samples after each execution boundary is reached
4. **Error handling**: Consecutive command failures (default: 3) stop the worker thread

### Example: Multi-Agent Scheduled Workflow

**Scenario**: Two agents with different schedules

**Agent 1** (`.guildbotics/config/team/members/agent1/person.yml`):
```yaml
person_id: agent1
name: Agent One
is_active: true
routine_commands:
  - workflows/ticket_driven_workflow
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
routine_commands:
  - workflows/code_review_check
task_schedules:
  - command: workflows/cleanup_old_branches
    schedules:
      - "0 0 * * 0"       # Sunday midnight
  - command: workflows/dependency_update_check
    schedules:
      - "?(0-59) 10 1 * *"  # First of month, random minute in 10 AM hour
```

Start both agents:
```bash
guildbotics start
```

Both agents will run concurrently, each executing their routine commands continuously and their scheduled tasks at the specified times.

# 6. GitHub Integration Example

This section describes how to use the default `ticket_driven_workflow` which integrates with GitHub Projects and Issues for ticket-based AI agent collaboration.

**Note**: This is one example use case. GuildBotics can be used for any scheduled automation tasks without GitHub integration.

## 6.1. Prerequisites

### 6.1.1. Git Environment
- Configure Git access for repositories:
  - HTTPS: Install GCM (Git Credential Manager) and sign in
  - or SSH: Set up SSH keys and `known_hosts`

### 6.1.2. Create a GitHub Project
Create a GitHub Projects (v2) project and add the following columns (statuses) in advance:
  - New
  - Ready
  - In Progress
  - In Review
  - Retrospective
  - Done

Note:
- For existing projects, you can map already-existing statuses to the above ones with the settings described later.
- If you do not use retrospectives, the Retrospective column is not required.

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

### 6.1.4. LLM API
Choose one of the following:
- Google Gemini API: Get your API key from [Google AI Studio](https://aistudio.google.com/app/apikey)
- OpenAI API: Get your API key from [OpenAI Platform](https://platform.openai.com/api-keys)
- Anthropic Claude API: Get your API key from [Anthropic Console](https://console.anthropic.com/settings/keys)

### 6.1.5. CLI Agent (Optional)
Install and sign in to one of the following CLI agents:
- [Gemini CLI](https://github.com/google-gemini/gemini-cli/)
- [OpenAI Codex CLI](https://github.com/openai/codex/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (requires Claude Pro or Max subscription)


## 6.2. Setup for GitHub Integration

After completing [Basic Usage](#5-basic-usage) steps, run the following to configure GitHub-specific settings:

```bash
guildbotics config verify
```

This command:
- Adds GuildBotics custom fields to GitHub Projects:
  - `Mode`: Behavior mode (comment/edit/ticket)
  - `Role`: Role to use for the task
  - `Agent`: AI agent to execute the task
- Maps GitHub Projects statuses to GuildBotics statuses (New/Ready/In Progress/In Review/Retrospective/Done)

## 6.3. Running the Ticket-Driven Workflow

### 6.3.1. Start
Start with:

```bash
guildbotics start [default_routine_commands...]
```

- `default_routine_commands` is a list of commands to execute routinely. If not specified, `workflows/ticket_driven_workflow` is used as the default.

This starts the task scheduler, allowing AI agents to execute tasks.

To stop the running scheduler:

```bash
guildbotics stop [--timeout <seconds>] [--force]
```

- Sends SIGTERM and waits up to `--timeout` seconds (default: 30).
- If it does not exit within the timeout and `--force` is specified, sends SIGKILL.
- If no scheduler is running, it reports the state and cleans up a stale pidfile if present.

For an immediate force stop:

```bash
guildbotics kill
```

This is equivalent to `guildbotics stop --force --timeout 0`.

### 6.3.2. How to Instruct the AI Agent

To request a task from the AI agent, operate the GitHub Projects ticket as follows:

1. Create a ticket, select the target Git repository, and save it as an Issue
2. Describe instructions to the AI agent in the ticket
   - This becomes the prompt to the agent, so be as specific as possible
3. Set the `Agent` field to select the AI agent that will execute the task
4. Set the `Mode` field
   - `comment`: Ask the agent to reply via ticket comments
   - `edit`: Ask the agent to edit files and open a Pull Request
   - `ticket`: Ask the agent to create tickets
5. Optionally set the `Role` field to specify the role to use when performing the task
6. Change the ticket status to `Ready`

Note:
The AI agent clones the specified Git repository under `~/.guildbotics/data/workspaces/<person_id>` and works there.

### 6.3.3. Interacting with the AI Agent
- If the AI agent has questions during work, it posts questions as ticket comments. Please respond in ticket comments. The agent periodically checks ticket comments and proceeds accordingly once answers are provided.
- When the AI agent completes a task, it changes the ticket status to `In Review` and posts the results and the created Pull Request URL as a comment.
- In `edit` mode, the AI agent creates a Pull Request. Please write review results as comments on the PR. When there are tickets in `In Review`, the agent checks for PR comments and responds accordingly if they exist.

## 6.4. Capabilities

With the ticket-driven workflow, you can:

- **Request tasks for AI agents on a task board**
  - Assign an AI agent to a ticket and move it to the **Ready** column to have the AI agent execute the task
- **Review AI agent results on the task board**
  - When the agent completes a task, the ticket moves to **In Review** and the results are posted as ticket comments
- **Create Pull Requests by AI agents**
  - When a task is completed, the AI agent creates a Pull Request
- **Create tickets**
  - If you instruct the AI agent to create tickets, it automatically creates them on the task board
- **Retrospective**
  - Move completed-task tickets to the **Retrospective** column and request a retrospective in a comment; the AI agent analyzes the interaction with reviewers on the created PR, extracts issues, and creates improvement tickets

# 7. Reference

## 7.1. Account-Related Environment Variables

**LLM API Keys**:
- `GOOGLE_API_KEY`: Google Gemini API
- `OPENAI_API_KEY`: OpenAI API
- `ANTHROPIC_API_KEY`: Anthropic Claude API

**GitHub Access** (per-person, format: `{PERSON_ID}_...`):
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: PAT for machine accounts/proxy agents
- `{PERSON_ID}_GITHUB_APP_ID`, `{PERSON_ID}_GITHUB_INSTALLATION_ID`, `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: For GitHub Apps

If a `.env` file exists in the current directory, it is loaded automatically.

## 7.2. Configuration Files

**Project Configuration** (`team/project.yml`):
- `language`: Project language
- `repositories`: Repository definitions
- `services.ticket_manager`: GitHub Projects settings
- `services.code_hosting_service`: GitHub repository settings

**Member Configuration** (`team/members/<person_id>/person.yml`):
- `person_id`: Unique identifier (lowercase alphanumeric, `-`, `_` only)
- `name`: Display name
- `is_active`: Whether the member acts as an AI agent
- `roles`: Role assignments
- `routine_commands`: Override default routine commands
- `task_schedules`: Cron-based scheduled commands

**Brain/CLI Agent Configuration**:
- `intelligences/cli_agent_mapping.yml`: Default CLI agent selection
- `intelligences/cli_agents/*.yml`: CLI agent scripts
- `team/members/<person_id>/intelligences/`: Per-agent overrides

## 7.3. Custom Commands

Create custom commands in `~/.guildbotics/config/commands/` (or project-local commands directory):

- **Markdown files** (`.md`): LLM prompts with optional frontmatter
- **Python files** (`.py`): Custom logic with context injection
- **Shell scripts** (`.sh`): Shell commands
- **YAML files** (`.yml`): Workflow composition

For detailed creation and operation, see the [Custom Command Development Guide](docs/custom_command_guide.en.md).

**Quick example**:
```markdown
<!-- translate.md -->
If the following text is in ${1}, translate it to ${2}; if it is in ${2}, translate it to ${1}:
```

Usage:
```bash
echo "Hello" | guildbotics run translate English Japanese
```


# 8. Troubleshooting

**Error Logs**: Check `~/.guildbotics/data/error.log` for details when errors occur.

**Debug Output**: Set environment variables for detailed logging:
- `LOG_LEVEL`: `debug` / `info` / `warning` / `error`
- `LOG_OUTPUT_DIR`: Directory to write log files (e.g., `./tmp/logs`)
- `AGNO_DEBUG`: Extra debug output for the Agno engine (`true`/`false`)

# 9. Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Coding style and conventions
- Testing guidelines
- Documentation standards
- Security best practices
