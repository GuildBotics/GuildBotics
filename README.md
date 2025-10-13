<h1>GuildBotics</h1>

[English](https://github.com/GuildBotics/GuildBotics/blob/main/README.md) • [日本語](https://github.com/GuildBotics/GuildBotics/blob/main/README.ja.md)

A tool to collaborate with AI agents via a task board.

---

## Important Notice (Disclaimer)

- This software is in alpha version. There is a very high possibility of breaking incompatible changes in the future, and malfunctions are expected to occur frequently, so use in production environments is not recommended.
- The author and distributor assume no responsibility for malfunctions of this software or damages caused thereby. In particular, due to malfunctions or runaway of AI agents, there is a possibility of fatal destruction to the system in use or external services, data loss, and leakage of confidential data. Use at your own risk and verify in an isolated test environment.

---

- [1. Capabilities](#1-capabilities)
- [2. Environment](#2-environment)
- [3. Supported Services / Software](#3-supported-services--software)
- [4. Prerequisites](#4-prerequisites)
  - [4.1. Git Environment](#41-git-environment)
  - [4.2. Create a GitHub Project](#42-create-a-github-project)
  - [4.3. Prepare a GitHub Account for the AI Agent](#43-prepare-a-github-account-for-the-ai-agent)
    - [4.3.1. Using a Machine Account (Machine User)](#431-using-a-machine-account-machine-user)
    - [4.3.2. Using a GitHub App](#432-using-a-github-app)
    - [4.3.3. Using Your Own Account as a Proxy Agent](#433-using-your-own-account-as-a-proxy-agent)
  - [4.4. Gemini API or OpenAI API](#44-gemini-api-or-openai-api)
  - [4.5. CLI Agent](#45-cli-agent)
- [5. Install and Set Up GuildBotics](#5-install-and-set-up-guildbotics)
  - [5.1. Initial Setup](#51-initial-setup)
  - [5.2. Add Members](#52-add-members)
  - [5.3. Verify Settings / Add Custom Fields / Map Statuses](#53-verify-settings--add-custom-fields--map-statuses)
    - [5.3.1. Add Custom Fields](#531-add-custom-fields)
    - [5.3.2. Status Mapping](#532-status-mapping)
- [6. Run](#6-run)
  - [6.1. Start](#61-start)
  - [6.2. How to Instruct the AI Agent](#62-how-to-instruct-the-ai-agent)
  - [6.3. Interacting with the AI Agent](#63-interacting-with-the-ai-agent)
- [7. Reference](#7-reference)
  - [7.1. Account-Related Environment Variables](#71-account-related-environment-variables)
    - [7.1.1. LLM API Variables](#711-llm-api-variables)
    - [7.1.2. GitHub Access Settings](#712-github-access-settings)
  - [7.2. Project Settings (`team/project.yml`)](#72-project-settings-teamprojectyml)
  - [7.3. Member Settings (`team/members/<person_id>/person.yml`)](#73-member-settings-teammembersperson_idpersonyml)
  - [7.4. Selecting a CLI Agent](#74-selecting-a-cli-agent)
  - [7.5. Modifying the CLI Agent Script](#75-modifying-the-cli-agent-script)
- [7.6. Per-Agent CLI Agent Settings](#76-per-agent-cli-agent-settings)
  - [7.7. Custom Command Prompts](#77-custom-command-prompts)
- [8. Troubleshooting](#8-troubleshooting)
  - [8.1. Error Logs](#81-error-logs)
  - [8.2. Obtaining Debug Information](#82-obtaining-debug-information)
- [9. Contributing](#9-contributing)

---

# 1. Capabilities
- Request tasks for AI agents on a task board
  - Assign an AI agent to a ticket and move it to the **Ready** column to have the AI agent execute the task.
- Review AI agent results on the task board
  - When the agent completes a task, the ticket moves to **In Review** and the results are posted as ticket comments.
- Create Pull Requests by AI agents
  - When a task is completed, the AI agent creates a Pull Request.
- Create tickets
  - If you instruct the AI agent to create tickets, it automatically creates them on the task board.
- Retrospective
  - Move completed-task tickets to the **Retrospective** column and request a retrospective in a comment; the AI agent analyzes the interaction with reviewers on the created PR, extracts issues, and creates improvement tickets.

# 2. Environment
- OS: Linux (verified on Ubuntu 24.04) / macOS (verified on Sequoia)
- Runtime: use `uv` (it automatically fetches/manages Python)

# 3. Supported Services / Software
The current version supports the following:

- Task board
  - GitHub Projects (Project v2)
- Code hosting service
  - GitHub
- CLI Agent
  - Google Gemini CLI
  - OpenAI Codex CLI
- LLM API
  - Google Gemini 2.5 Flash
  - OpenAI GPT-5 Mini

# 4. Prerequisites
## 4.1. Git Environment
- Configure Git access for repositories:
  - HTTPS: Install GCM (Git Credential Manager) and sign in
  - or SSH: Set up SSH keys and `known_hosts`

## 4.2. Create a GitHub Project
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

## 4.3. Prepare a GitHub Account for the AI Agent
Prepare an account the AI agent will use to access GitHub. You can choose one of the following:

- **Machine Account** (Machine User)
  - Recommended if you want to keep the “work with an AI agent via the task board and Pull Requests” feel. However, per the [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service#3-account-requirements), free machine accounts are limited to one per user.
- **GitHub App**
  - There is no limit on the number of apps, but it cannot access GitHub Projects owned by a personal account. Also, GitHub UI clearly marks the app as a bot, which slightly changes the feel.
- Use your own account as a **proxy agent**
  - The simplest option. The visual impression is more like “talking to yourself” than interacting with a separate AI agent.

### 4.3.1. Using a Machine Account (Machine User)
After creating a machine account, do the following:

1. Add the machine account as a Collaborator to the project and repositories.
2. Issue a Classic PAT
  - Issue a **Classic** Personal Access Token.
  - Select the scopes `repo` and `project`.

### 4.3.2. Using a GitHub App
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

### 4.3.3. Using Your Own Account as a Proxy Agent
If you use your own account as the AI agent, issue a **Classic** PAT. Select the scopes `repo` and `project`.

## 4.4. Gemini API or OpenAI API
Obtain a Gemini API key or an OpenAI API key.

## 4.5. CLI Agent
Install and sign in to either [Gemini CLI](https://github.com/google-gemini/gemini-cli/) or [OpenAI Codex CLI](https://github.com/openai/codex/).


# 5. Install and Set Up GuildBotics
You can install with the following:

```bash
uv tool install guildbotics
```

## 5.1. Initial Setup

Run the following for initial setup:

```bash
guildbotics init
```

`guildbotics init` is interactive in your terminal and then generates configuration files. You will:

- Select language
  - English or Japanese
- Select the configuration directory
  - Either under the home directory or under the current directory
- Create or update an environment file
  - Choose to create, append to, or overwrite the `.env` file
- Select LLM API
  - Choose Gemini API or OpenAI API
- Select a CLI agent
  - Choose Gemini CLI or OpenAI Codex CLI
- Select repository access method
  - Choose HTTPS or SSH for Git operations
- Enter the GitHub Project and repository URLs
  - Enter the GitHub Projects URL and the repository URL

The following files are created/updated:

- Current directory
  - `.env` is created/updated with environment variables
- `.guildbotics/config/` under the home directory or under the current directory
  - Project definition: `team/project.yml`
  - CLI agent mapping: `intelligences/cli_agent_mapping.yml`
  - CLI agent script definitions (one of):
    - `intelligences/cli_agents/codex-cli.yml`
    - `intelligences/cli_agents/gemini-cli.yml`


## 5.2. Add Members

Add members with:

```bash
guildbotics add
```

`guildbotics add` interactively prompts for member (AI agent or human) information and generates configuration files.

- Select member type
  - Choose one of: human, machine account, GitHub App, proxy agent (use your own account)
    - “Human” only provides member information to the AI agent and does not act as an AI agent.
- Enter GitHub username (for human, machine account, GitHub App, proxy agent)
  - For proxy agent, enter your own GitHub username
- Enter the GitHub App URL (for GitHub App)
- Enter the member ID (person_id) used in GuildBotics (all types)
  - Lowercase alphanumeric only; defaults to the GitHub username
- Enter the display name (all types)
  - Full name of the member
- Select roles (all types)
  - Choose roles such as product owner, project manager, architect (multiple can be selected)
- Select speaking style (for machine account, GitHub App, proxy agent)
  - Choose friendly, professional, or machine
- Set environment variables
  - For GitHub App: input installation ID, App ID, and the private key path
  - For machine account: input PAT

Two types of files are created/updated:

- Current directory
  - `.env` is updated with environment variables; stores secrets such as PAT or GitHub App settings
- `.guildbotics/config/` under home or current directory
  - Member definition: `team/members/<person_id>/person.yml`

If you want to add multiple AI agents, repeat the same steps for each.

Note:
`person_id` is the identifier used within GuildBotics. Because it is used in environment variable names and directory names, use only lowercase alphanumeric characters. Characters other than "-" and "_" or whitespace are not allowed.


## 5.3. Verify Settings / Add Custom Fields / Map Statuses

Run the following to verify configuration and perform the steps below:

- Add GuildBotics-specific custom fields to GitHub Projects
- Map GitHub Projects status columns

```bash
guildbotics verify
```

### 5.3.1. Add Custom Fields
The following custom fields (all single-select) are added to GitHub Projects:

- `Mode`: Select the behavior mode of the AI agent
  - `comment`: Responds to ticket instructions via comments
  - `edit`: Edits files based on ticket instructions and creates a Pull Request
  - `ticket`: Creates tickets
- `Role`: Role to use when performing the work described in the ticket
- `Agent`: Select the AI agent to execute the task

### 5.3.2. Status Mapping
Map GitHub Projects statuses to the statuses used by GuildBotics.

GuildBotics uses the following six statuses:
  - New
    - Set by the AI agent when the user requests ticket creation in `ticket` mode
  - Ready
    - Set by the user when requesting the AI agent to execute a task
  - In Progress
    - Set by the AI agent when it starts working on the task
  - In Review
    - Set by the AI agent when it completes the task
  - Retrospective
    - Set by the user when requesting a retrospective from the AI agent
  - Done
    - Set by the user when they deem the task complete

The status mapping is saved to `.guildbotics/config/team/project.yml`.


# 6. Run
## 6.1. Start
Start with:

```bash
guildbotics start
```

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

## 6.2. How to Instruct the AI Agent

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

## 6.3. Interacting with the AI Agent
- If the AI agent has questions during work, it posts questions as ticket comments. Please respond in ticket comments. The agent periodically checks ticket comments and proceeds accordingly once answers are provided.
- When the AI agent completes a task, it changes the ticket status to `In Review` and posts the results and the created Pull Request URL as a comment.
- In `edit` mode, the AI agent creates a Pull Request. Please write review results as comments on the PR. When there are tickets in `In Review`, the agent checks for PR comments and responds accordingly if they exist.


# 7. Reference
## 7.1. Account-Related Environment Variables

GuildBotics uses the following environment variables:

- `GOOGLE_API_KEY`: Required to use the Gemini API
- `{PERSON_ID}_GITHUB_ACCESS_TOKEN`: Personal Access Token for machine accounts
- `{PERSON_ID}_GITHUB_APP_ID`: GitHub App ID
- `{PERSON_ID}_GITHUB_INSTALLATION_ID`: GitHub App installation ID
- `{PERSON_ID}_GITHUB_PRIVATE_KEY_PATH`: Path to the GitHub App private key

If a `.env` file exists, it is loaded automatically.

### 7.1.1. LLM API Variables

Set the Gemini API key to `GOOGLE_API_KEY`.

```bash
export GOOGLE_API_KEY=your_google_api_key
```

Set the OpenAI API key to `OPENAI_API_KEY`.

```bash
export OPENAI_API_KEY=your_openai_api_key
```

### 7.1.2. GitHub Access Settings

Per-person secrets are referenced as `${PERSON_ID_UPPER}_${KEY_UPPER}` (example: `person_id: yuki`).

- For machine users and proxy agents:

  ```bash
  export YUKI_GITHUB_ACCESS_TOKEN=ghp_xxx
  ```

- For GitHub Apps:

  ```bash
  export YUKI_GITHUB_APP_ID=123456
  export YUKI_GITHUB_INSTALLATION_ID=987654321
  export YUKI_GITHUB_PRIVATE_KEY_PATH=/absolute/path/to/your-app-private-key.pem
  ```

## 7.2. Project Settings (`team/project.yml`)
- `team/project.yml`:
  - `language`: Project language (e.g., `ja`, `en`)
  - `repositories.name`: Repository name
  - `services.ticket_manager.name`: `GitHub` (not changeable)
  - `services.ticket_manager.owner`: GitHub user/organization name
  - `services.ticket_manager.project_id`: GitHub Projects (v2) Project number
  - `services.ticket_manager.url`: URL of the above project
  - `services.code_hosting_service.repo_base_url`: Base URL used for cloning
    - Example: `https://github.com` (HTTPS) or `ssh://git@github.com` (SSH)

## 7.3. Member Settings (`team/members/<person_id>/person.yml`)
- `team/members/<person_id>/person.yml`:
  - `person_id`: Member ID (lowercase alphanumeric; symbols and spaces not allowed)
  - `name`: Member name (full name)
  - `is_active`: Whether the member can act as an AI agent (true/false)
  - `person_type`: Member type (e.g., human/machine_user/github_apps/proxy_agent)
  - `account_info.github_username`: GitHub username
  - `account_info.git_user`: Git user name
  - `account_info.git_email`: Git email address
  - `profile`: Per-role profile settings. Use role IDs as keys (e.g., professional, programmer, product_owner) and optionally provide `summary`/`description`. Even an empty map (e.g., `product_owner:`) enables that role and merges with predefined roles (e.g., `roles/default.*.yml`).
  - `speaking_style`: Description of speaking style
  - `relationships`: Description of relationships with other members


## 7.4. Selecting a CLI Agent

Switch CLI agents by editing `intelligences/cli_agent_mapping.yml`.

Using Codex CLI:

```yaml
default: codex-cli.yml
```

Using Gemini CLI:

```yaml
default: gemini-cli.yml
```

## 7.5. Modifying the CLI Agent Script
Customize the CLI agent invocation by editing the YAML files under `intelligences/cli_agents`.

## 7.6. Per-Agent CLI Agent Settings
By default, all AI agents share the same CLI agent, but if `team/members/<person_id>/intelligences/cli_agent_mapping.yml` and/or `team/members/<person_id>/intelligences/cli_agents/*.yml` exist, they take precedence. This allows changing the CLI agent per member (AI agent).


## 7.7. Custom Command Prompts
> For detailed creation and operation, see docs/custom_command_guide.en.md
You can embed and execute custom command prompts in ticket descriptions or comments by writing a line that starts with `//`.

- Write in the ticket: `//<name> [args...]`
  - Example: `//code-review target=src/app.py level=deep`
  - A trailing colon after the name is allowed: `//code-review: target=...`
- The system replaces that line with the body of a prompt file before the agent runs.

Prompt file resolution order (first match wins):
- Per-person: `.guildbotics/config/team/members/<person_id>/prompts/<name>.<lang>.md`, then `<name>.en.md`, then `<name>.md`
- Project-wide: `.guildbotics/config/prompts/<name>.<lang>.md`, then `<name>.en.md`, then `<name>.md`
- Fallback: `.guildbotics/config/intelligences/<name>.md`

Prompt file format: Markdown with YAML front matter. The body is used as the expanded text.

```markdown
---
template_engine: jinja2  # or "default"
---
# Review Plan for {{ target }}

Priority: {{ level | default('normal') }}
Steps:
- Run static analysis on {{ target }}
- Summarize hotspots and tests to add
```

Argument passing and templating:
- Key-value args: `//review target="src/app.py" level=deep`
- Quoted values are supported (spaces preserved).
- Positional args are also supported:
  - With `template_engine: jinja2`, use `{{ arg1 }}`, `{{ arg2 }}`, ...
  - With `template_engine: default`, use placeholders like `{{1}}`, `{1}`, `${1}`, or `$1` (named keys also work: `{{target}}`, `{target}`, `${target}`, `$target`).

-Usage tips:
- If you add a comment, write the `//<name> ...` line in the newest (last) comment.
- Create reusable prompts under `.guildbotics/config/prompts/` and override per agent under `.guildbotics/config/team/members/<person_id>/prompts/`.


# 8. Troubleshooting
## 8.1. Error Logs
If an unexpected error occurs during task execution, a comment saying "An error occurred while executing the task. Please check the error log for details." is added to the ticket. The error log at `~/.guildbotics/data/error.log` contains details of the error.

## 8.2. Obtaining Debug Information
Set the following environment variables to obtain debug information:

- `AGNO_DEBUG`: Extra debug output for the `agno` engine (`true`/`false`).
- `LOG_LEVEL`: Log level (`debug` / `info` / `warning` / `error`).
- `LOG_OUTPUT_DIR`: Log output directory (e.g., `./tmp/logs`). If set, logs printed to the console are also written to files under the specified directory.


# 9. Contributing
We welcome Pull Requests (PRs). We welcome any contributions, such as adding new features, bug fixes, documentation improvements, etc.

Please read [CONTRIBUTING.md](https://github.com/GuildBotics/GuildBotics/blob/main/CONTRIBUTING.md) for coding style, testing, documentation, and security guidelines before opening a PR.
