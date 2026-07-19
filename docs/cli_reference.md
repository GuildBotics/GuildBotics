# GuildBotics CLI Reference

<!-- AUTO-GENERATED FILE. DO NOT EDIT BY HAND. -->

This reference is generated from the Click definitions in `guildbotics/cli/`.
To regenerate it after changing the CLI, run:

```bash
uv run --no-sync python scripts/generate-cli-reference.py
```

For concepts (workspaces, custom commands, scheduling, secrets), see the
[README](../README.md) and the
[Custom Command Development Guide](custom_command_guide.en.md).

## Command index

| Command | Summary |
| --- | --- |
| [`guildbotics`](#guildbotics) | GuildBotics CLI entrypoint. |
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill a CLI-managed background service. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
| [`guildbotics member agent`](#guildbotics-member-agent) | Manage native agent runtime state. |
| [`guildbotics member agent conversation`](#guildbotics-member-agent-conversation) | Manage persisted native agent conversations. |
| [`guildbotics member agent conversation reset`](#guildbotics-member-agent-conversation-reset) | Reset one exact native provider session without deleting history. |
| [`guildbotics member chat`](#guildbotics-member-chat) | Chat identity, posting, replies, reactions, and run completion. |
| [`guildbotics member chat complete`](#guildbotics-member-chat-complete) | Finish a chat workflow run with evidence. |
| [`guildbotics member chat identity`](#guildbotics-member-chat-identity) | Show the member's chat identity. |
| [`guildbotics member chat inspect`](#guildbotics-member-chat-inspect) | Inspect Slack channel or thread messages for interactive decisions. |
| [`guildbotics member chat inspect channel`](#guildbotics-member-chat-inspect-channel) | Read recent channel messages. |
| [`guildbotics member chat inspect thread`](#guildbotics-member-chat-inspect-thread) | Read a thread before replying or reacting. |
| [`guildbotics member chat noop`](#guildbotics-member-chat-noop) | Record a deliberate no-op for a chat event. |
| [`guildbotics member chat post`](#guildbotics-member-chat-post) | Post a new channel message. |
| [`guildbotics member chat reaction`](#guildbotics-member-chat-reaction) | Chat reaction operations. |
| [`guildbotics member chat reaction add`](#guildbotics-member-chat-reaction-add) | Add a semantic reaction. |
| [`guildbotics member chat reply`](#guildbotics-member-chat-reply) | Reply in a thread in the member voice. |
| [`guildbotics member context`](#guildbotics-member-context) | Show non-secret member context. |
| [`guildbotics member git`](#guildbotics-member-git) | Prepare, commit, push, and publish member git workspaces. |
| [`guildbotics member git commit`](#guildbotics-member-git-commit) | Commit already-staged changes with the member identity. |
| [`guildbotics member git prepare`](#guildbotics-member-git-prepare) | Clone/checkout an isolated member workspace: a ticket branch (--issue-url), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo... |
| [`guildbotics member git publish`](#guildbotics-member-git-publish) | Commit already-staged changes with the member identity, then push. |
| [`guildbotics member git push`](#guildbotics-member-git-push) | Push the current branch with the member credential. |
| [`guildbotics member github`](#guildbotics-member-github) | GitHub issue, pull request, and reaction capabilities. |
| [`guildbotics member github issue`](#guildbotics-member-github-issue) | GitHub issue operations. |
| [`guildbotics member github issue comment`](#guildbotics-member-github-issue-comment) | Comment on an issue in the member voice. |
| [`guildbotics member github issue create`](#guildbotics-member-github-issue-create) | Open a follow-up issue. |
| [`guildbotics member github issue inspect`](#guildbotics-member-github-issue-inspect) | Read an issue and its comments. |
| [`guildbotics member github issue update`](#guildbotics-member-github-issue-update) | Replace an issue body; an empty stdin removes the body. |
| [`guildbotics member github pr`](#guildbotics-member-github-pr) | GitHub pull request operations. |
| [`guildbotics member github pr comment`](#guildbotics-member-github-pr-comment) | Comment on a PR conversation. |
| [`guildbotics member github pr create`](#guildbotics-member-github-pr-create) | Open a PR, or return the existing open PR for the same head and base branches. |
| [`guildbotics member github pr inspect`](#guildbotics-member-github-pr-inspect) | Read a PR, optionally including review threads and diff comment coordinates. |
| [`guildbotics member github pr reply`](#guildbotics-member-github-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member github pr review-comment`](#guildbotics-member-github-pr-review-comment) | Create a new inline review comment on a PR diff line. |
| [`guildbotics member github pr update`](#guildbotics-member-github-pr-update) | Replace a PR body; an empty stdin removes the body. |
| [`guildbotics member github reaction`](#guildbotics-member-github-reaction) | GitHub reaction operations. |
| [`guildbotics member github reaction add`](#guildbotics-member-github-reaction-add) | React to an issue or review comment. |
| [`guildbotics member help`](#guildbotics-member-help) | Print the member capability reference (commands and cross-cutting rules). |
| [`guildbotics member memory`](#guildbotics-member-memory) | Record, recall, and maintain member memory documents. |
| [`guildbotics member memory archive`](#guildbotics-member-memory-archive) | Move a stale memory under archived/ and remove it from recall and digest. |
| [`guildbotics member memory get`](#guildbotics-member-memory-get) | Read one memory document's metadata, body, and asset paths without changing recency. |
| [`guildbotics member memory promote`](#guildbotics-member-memory-promote) | Move a personal memory into team memory without changing the document id. |
| [`guildbotics member memory recall`](#guildbotics-member-memory-recall) | Search personal and team memory by literal OR queries and return compact hits. |
| [`guildbotics member memory record`](#guildbotics-member-memory-record) | Create a memory document and move it to the front of the digest. |
| [`guildbotics member memory touch`](#guildbotics-member-memory-touch) | Mark a useful memory as actually used by moving it to the digest front. |
| [`guildbotics member memory update`](#guildbotics-member-memory-update) | Replace selected body or metadata fields and move the document to the digest front. |
| [`guildbotics member task`](#guildbotics-member-task) | Workflow task-run completion records. |
| [`guildbotics member task complete`](#guildbotics-member-task-complete) | Finish a ticket workflow run with evidence. |
| [`guildbotics member task status`](#guildbotics-member-task-status) | Inspect recorded run evidence. |
| [`guildbotics run`](#guildbotics-run) | Run the GuildBotics application. |
| [`guildbotics secrets`](#guildbotics-secrets) | Manage workspace secrets (API keys and tokens). |
| [`guildbotics secrets delete`](#guildbotics-secrets-delete) | Delete a stored secret. |
| [`guildbotics secrets export`](#guildbotics-secrets-export) | Export stored secrets in dotenv format (for moving machines). |
| [`guildbotics secrets import`](#guildbotics-secrets-import) | Import secrets from a dotenv-format file into the workspace store. |
| [`guildbotics secrets list`](#guildbotics-secrets-list) | List the names of the stored secrets. |
| [`guildbotics secrets set`](#guildbotics-secrets-set) | Store a secret value (prompts when VALUE is omitted). |
| [`guildbotics secrets status`](#guildbotics-secrets-status) | Show the secret backend used by this workspace. |
| [`guildbotics start`](#guildbotics-start) | Start GuildBotics runtimes (scheduler and event listener runner). |
| [`guildbotics stop`](#guildbotics-stop) | Gracefully stop a CLI-managed background service. |
| [`guildbotics version`](#guildbotics-version) | Print version. |
| [`guildbotics workspace`](#guildbotics-workspace) | Manage the active GuildBotics workspace used by AI CLI tools. |
| [`guildbotics workspace current`](#guildbotics-workspace-current) | Show the persisted active workspace. |
| [`guildbotics workspace status`](#guildbotics-workspace-status) | Show active workspace status without failing when it is missing. |
| [`guildbotics workspace use`](#guildbotics-workspace-use) | Persist the active workspace for desktop and external AI CLI tools. |

## `guildbotics`

GuildBotics CLI entrypoint.

```text
guildbotics [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--version` | Show the version and exit. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill a CLI-managed background service. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
| [`guildbotics run`](#guildbotics-run) | Run the GuildBotics application. |
| [`guildbotics secrets`](#guildbotics-secrets) | Manage workspace secrets (API keys and tokens). |
| [`guildbotics start`](#guildbotics-start) | Start GuildBotics runtimes (scheduler and event listener runner). |
| [`guildbotics stop`](#guildbotics-stop) | Gracefully stop a CLI-managed background service. |
| [`guildbotics version`](#guildbotics-version) | Print version. |
| [`guildbotics workspace`](#guildbotics-workspace) | Manage the active GuildBotics workspace used by AI CLI tools. |

## `guildbotics kill`

Immediately force kill a CLI-managed background service.

Equivalent to: `guildbotics stop --force --timeout 0`.

```text
guildbotics kill [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics member`

Operate as a configured GuildBotics member.

```text
guildbotics member [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--workspace DIRECTORY` | Workspace root to use instead of the persisted active workspace. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member agent`](#guildbotics-member-agent) | Manage native agent runtime state. |
| [`guildbotics member chat`](#guildbotics-member-chat) | Chat identity, posting, replies, reactions, and run completion. |
| [`guildbotics member context`](#guildbotics-member-context) | Show non-secret member context. |
| [`guildbotics member git`](#guildbotics-member-git) | Prepare, commit, push, and publish member git workspaces. |
| [`guildbotics member github`](#guildbotics-member-github) | GitHub issue, pull request, and reaction capabilities. |
| [`guildbotics member help`](#guildbotics-member-help) | Print the member capability reference (commands and cross-cutting rules). |
| [`guildbotics member memory`](#guildbotics-member-memory) | Record, recall, and maintain member memory documents. |
| [`guildbotics member task`](#guildbotics-member-task) | Workflow task-run completion records. |

## `guildbotics member agent`

Manage native agent runtime state.

```text
guildbotics member agent [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member agent conversation`](#guildbotics-member-agent-conversation) | Manage persisted native agent conversations. |

## `guildbotics member agent conversation`

Manage persisted native agent conversations.

```text
guildbotics member agent conversation [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member agent conversation reset`](#guildbotics-member-agent-conversation-reset) | Reset one exact native provider session without deleting history. |

## `guildbotics member agent conversation reset`

Reset one exact native provider session without deleting history.

```text
guildbotics member agent conversation reset [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--adapter [codex\|claude]` | Native provider adapter whose exact session should be reset. [required] |
| `--work-kind [ticket\|chat\|manual]` | Kind of logical work owning the conversation. [required] |
| `--work-identity TEXT` | Stable ticket URL, chat thread identity, or manual work identity. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat`

Chat identity, posting, replies, reactions, and run completion.

```text
guildbotics member chat [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member chat complete`](#guildbotics-member-chat-complete) | Finish a chat workflow run with evidence. |
| [`guildbotics member chat identity`](#guildbotics-member-chat-identity) | Show the member's chat identity. |
| [`guildbotics member chat inspect`](#guildbotics-member-chat-inspect) | Inspect Slack channel or thread messages for interactive decisions. |
| [`guildbotics member chat noop`](#guildbotics-member-chat-noop) | Record a deliberate no-op for a chat event. |
| [`guildbotics member chat post`](#guildbotics-member-chat-post) | Post a new channel message. |
| [`guildbotics member chat reaction`](#guildbotics-member-chat-reaction) | Chat reaction operations. |
| [`guildbotics member chat reply`](#guildbotics-member-chat-reply) | Reply in a thread in the member voice. |

## `guildbotics member chat complete`

Finish a chat workflow run with evidence.

```text
guildbotics member chat complete [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--run-id TEXT` | Workflow run id. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the triggering event. [required] |
| `--thread-ts TEXT` | Thread timestamp of the triggering event. [required] |
| `--event-id TEXT` | Event id of the chat trigger. [required] |
| `--status [done\|asking\|blocked]` | Run outcome. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat identity`

Show the member's chat identity.

```text
guildbotics member chat identity [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics member chat inspect`

Inspect Slack channel or thread messages for interactive decisions.

```text
guildbotics member chat inspect [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member chat inspect channel`](#guildbotics-member-chat-inspect-channel) | Read recent channel messages. |
| [`guildbotics member chat inspect thread`](#guildbotics-member-chat-inspect-thread) | Read a thread before replying or reacting. |

## `guildbotics member chat inspect channel`

Read recent channel messages.

```text
guildbotics member chat inspect channel [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--oldest-ts TEXT` | Only include messages at or after this timestamp. [default: ""] |
| `--latest-ts TEXT` | Only include messages at or before this timestamp. [default: ""] |
| `--limit INTEGER RANGE` | Maximum number of messages. [default: 50; 1\<=x\<=200] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat inspect thread`

Read a thread before replying or reacting.

```text
guildbotics member chat inspect thread [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--thread-ts TEXT` | Thread timestamp (with --channel-id). [default: ""] |
| `--message-url TEXT` | Slack message URL (alternative to channel/timestamp options). [default: ""] |
| `--limit INTEGER RANGE` | Maximum number of messages. [default: 100; 1\<=x\<=200] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat noop`

Record a deliberate no-op for a chat event.

```text
guildbotics member chat noop [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--run-id TEXT` | Workflow run id. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the triggering event. [required] |
| `--thread-ts TEXT` | Thread timestamp of the triggering event. [required] |
| `--event-id TEXT` | Event id of the chat trigger. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat post`

Post a new channel message.

```text
guildbotics member chat post [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat reaction`

Chat reaction operations.

```text
guildbotics member chat reaction [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member chat reaction add`](#guildbotics-member-chat-reaction-add) | Add a semantic reaction. |

## `guildbotics member chat reaction add`

Add a semantic reaction.

```text
guildbotics member chat reaction add [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--message-ts TEXT` | Message timestamp (with --channel-id). [default: ""] |
| `--message-url TEXT` | Slack message URL (alternative to channel/timestamp options). [default: ""] |
| `--reaction [ack\|agree\|celebrate\|support]` | Semantic reaction to add. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat reply`

Reply in a thread in the member voice.

```text
guildbotics member chat reply [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--service [slack]` | Chat service to use. [default: slack] |
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--thread-ts TEXT` | Thread timestamp (with --channel-id). [default: ""] |
| `--message-url TEXT` | Slack message URL (alternative to channel/timestamp options). [default: ""] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member context`

Show non-secret member context.

```text
guildbotics member context [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--check-credentials` | Also verify the member's provider credentials. |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics member git`

Prepare, commit, push, and publish member git workspaces.

```text
guildbotics member git [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member git commit`](#guildbotics-member-git-commit) | Commit already-staged changes with the member identity. |
| [`guildbotics member git prepare`](#guildbotics-member-git-prepare) | Clone/checkout an isolated member workspace: a ticket branch (--issue-url), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo... |
| [`guildbotics member git publish`](#guildbotics-member-git-publish) | Commit already-staged changes with the member identity, then push. |
| [`guildbotics member git push`](#guildbotics-member-git-push) | Push the current branch with the member credential. |

## `guildbotics member git commit`

Commit already-staged changes with the member identity.

Stage the files you want with plain git (e.g. ``git add``) first; this
command commits only what is staged and applies the member name/email to
that single commit without changing the repository's git config.

```text
guildbotics member git commit [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo-path PATH` | Path to the member repository workspace. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. [default: member] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member git prepare`

Clone/checkout an isolated member workspace: a ticket branch (--issue-url), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo --branch). --repo cannot be combined with the URL options.

```text
guildbotics member git prepare [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--issue-url TEXT` | Ticket issue URL to prepare a workspace for. [default: ""] |
| `--pr-url TEXT` | PR URL whose head branch to check out. [default: ""] |
| `--repo TEXT` | Target repository as \<owner\>/\<repo\>. [default: ""] |
| `--branch TEXT` | Branch to create or check out (with --repo). [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member git publish`

Commit already-staged changes with the member identity, then push.

Stage the files you want with plain git (e.g. ``git add``) first; this
commits only what is staged with the member name/email and pushes the
branch using the member credential.

```text
guildbotics member git publish [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo-path PATH` | Path to the member repository workspace. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. [default: member] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member git push`

Push the current branch with the member credential.

```text
guildbotics member git push [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo-path PATH` | Path to the member repository workspace. [required] |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. [default: member] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github`

GitHub issue, pull request, and reaction capabilities.

```text
guildbotics member github [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member github issue`](#guildbotics-member-github-issue) | GitHub issue operations. |
| [`guildbotics member github pr`](#guildbotics-member-github-pr) | GitHub pull request operations. |
| [`guildbotics member github reaction`](#guildbotics-member-github-reaction) | GitHub reaction operations. |

## `guildbotics member github issue`

GitHub issue operations.

```text
guildbotics member github issue [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member github issue comment`](#guildbotics-member-github-issue-comment) | Comment on an issue in the member voice. |
| [`guildbotics member github issue create`](#guildbotics-member-github-issue-create) | Open a follow-up issue. |
| [`guildbotics member github issue inspect`](#guildbotics-member-github-issue-inspect) | Read an issue and its comments. |
| [`guildbotics member github issue update`](#guildbotics-member-github-issue-update) | Replace an issue body; an empty stdin removes the body. |

## `guildbotics member github issue comment`

Comment on an issue in the member voice.

```text
guildbotics member github issue comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Issue URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github issue create`

Open a follow-up issue.

```text
guildbotics member github issue create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo TEXT` | Target repository as \<owner\>/\<repo\>. [required] |
| `--title TEXT` | Issue title. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--add-to-project / --no-add-to-project` | Add the created issue to the configured project board. [default: add-to-project] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github issue inspect`

Read an issue and its comments.

```text
guildbotics member github issue inspect [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Issue URL. [required] |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics member github issue update`

Replace an issue body; an empty stdin removes the body.

```text
guildbotics member github issue update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Issue URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr`

GitHub pull request operations.

```text
guildbotics member github pr [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member github pr comment`](#guildbotics-member-github-pr-comment) | Comment on a PR conversation. |
| [`guildbotics member github pr create`](#guildbotics-member-github-pr-create) | Open a PR, or return the existing open PR for the same head and base branches. |
| [`guildbotics member github pr inspect`](#guildbotics-member-github-pr-inspect) | Read a PR, optionally including review threads and diff comment coordinates. |
| [`guildbotics member github pr reply`](#guildbotics-member-github-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member github pr review-comment`](#guildbotics-member-github-pr-review-comment) | Create a new inline review comment on a PR diff line. |
| [`guildbotics member github pr update`](#guildbotics-member-github-pr-update) | Replace a PR body; an empty stdin removes the body. |

## `guildbotics member github pr comment`

Comment on a PR conversation.

```text
guildbotics member github pr comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr create`

Open a PR, or return the existing open PR for the same head and base branches.

```text
guildbotics member github pr create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo TEXT` | Target repository as \<owner\>/\<repo\>. [required] |
| `--head TEXT` | Head branch containing the changes. [required] |
| `--base TEXT` | Base branch for the pull request. Defaults to the repository default branch. [default: ""] |
| `--title TEXT` | Pull request title. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--issue-url TEXT` | Related issue URL to link to the PR. [default: ""] |
| `--draft [auto\|true\|false]` | Open as a draft PR; 'auto' drafts when the member is a proxy agent. [default: auto] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr inspect`

Read a PR, optionally including review threads and diff comment coordinates.

```text
guildbotics member github pr inspect [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--include-comments` | Include review threads with their reply target ids. |
| `--include-diff` | Include the diff with commentable line coordinates. |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr reply`

Reply to an inline review thread.

```text
guildbotics member github pr reply [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--reply-target-id INTEGER` | reply_target_id from 'pr inspect --include-comments'. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr review-comment`

Create a new inline review comment on a PR diff line.

```text
guildbotics member github pr review-comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--path TEXT` | File path in the PR diff. [required] |
| `--line INTEGER RANGE` | Line number on the chosen diff side. [x\>=1; required] |
| `--side [LEFT\|RIGHT]` | Diff side of the line. [default: RIGHT] |
| `--start-line INTEGER RANGE` | Start line for a multi-line comment. [x\>=1] |
| `--start-side [LEFT\|RIGHT]` | Diff side of --start-line. |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github pr update`

Replace a PR body; an empty stdin removes the body.

```text
guildbotics member github pr update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member github reaction`

GitHub reaction operations.

```text
guildbotics member github reaction [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member github reaction add`](#guildbotics-member-github-reaction-add) | React to an issue or review comment. |

## `guildbotics member github reaction add`

React to an issue or review comment.

```text
guildbotics member github reaction add [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo TEXT` | Target repository as \<owner\>/\<repo\>. [required] |
| `--target [issue-comment\|pr-review-comment]` | Kind of comment to react to. [required] |
| `--comment-id INTEGER` | Numeric id of the comment. [required] |
| `--reaction [+1\|eyes\|heart\|hooray\|rocket\|laugh\|confused\|-1]` | Reaction to add. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member help`

Print the member capability reference (commands and cross-cutting rules).

This is the same reference embedded in ``member context``; use it to reread
the available commands without re-running the full context.

```text
guildbotics member help [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics member memory`

Record, recall, and maintain member memory documents.

```text
guildbotics member memory [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member memory archive`](#guildbotics-member-memory-archive) | Move a stale memory under archived/ and remove it from recall and digest. |
| [`guildbotics member memory get`](#guildbotics-member-memory-get) | Read one memory document's metadata, body, and asset paths without changing recency. |
| [`guildbotics member memory promote`](#guildbotics-member-memory-promote) | Move a personal memory into team memory without changing the document id. |
| [`guildbotics member memory recall`](#guildbotics-member-memory-recall) | Search personal and team memory by literal OR queries and return compact hits. |
| [`guildbotics member memory record`](#guildbotics-member-memory-record) | Create a memory document and move it to the front of the digest. |
| [`guildbotics member memory touch`](#guildbotics-member-memory-touch) | Mark a useful memory as actually used by moving it to the digest front. |
| [`guildbotics member memory update`](#guildbotics-member-memory-update) | Replace selected body or metadata fields and move the document to the digest front. |

## `guildbotics member memory archive`

Move a stale memory under archived/ and remove it from recall and digest.

```text
guildbotics member memory archive [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--id TEXT` | Memory document id. [required] |
| `--team` | Operate on team memory instead of personal memory. |
| `--policy-approved` | Confirm that a human approved this policy memory change. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory get`

Read one memory document's metadata, body, and asset paths without changing recency.

```text
guildbotics member memory get [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--id TEXT` | Memory document id. [required] |
| `--team` | Operate on team memory instead of personal memory. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory promote`

Move a personal memory into team memory without changing the document id.

```text
guildbotics member memory promote [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--id TEXT` | Memory document id. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory recall`

Search personal and team memory by literal OR queries and return compact hits.

```text
guildbotics member memory recall [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--query TEXT` | Literal search query; repeat for OR matching. |
| `--meta-only` | Return hit metadata without body excerpts. |
| `--limit INTEGER RANGE` | Maximum number of hits. [default: 20; 1\<=x\<=200] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory record`

Create a memory document and move it to the front of the digest.

```text
guildbotics member memory record [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--scope [personal\|team]` | Store as personal or team memory. [default: personal] |
| `--title TEXT` | Document title. [required] |
| `--summary TEXT` | One-line summary shown in recall hits and the digest. [default: ""] |
| `--keyword TEXT` | Recall keyword. May be repeated. |
| `--ticket TEXT` | Related ticket URL (source anchor). May be repeated. |
| `--pr TEXT` | Related PR URL (source anchor). May be repeated. |
| `--channel TEXT` | Related chat channel URL (source anchor). May be repeated. |
| `--thread TEXT` | Related chat thread URL (source anchor). May be repeated. |
| `--kind [note\|policy]` | Document kind; 'policy' requires --policy-approved. [default: note] |
| `--pin` | Pin as a standing rule included in member context. |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--policy-approved` | Confirm that a human approved this policy memory change. |
| `--set TEXT` | Extra metadata as key=value. May be repeated. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory touch`

Mark a useful memory as actually used by moving it to the digest front.

```text
guildbotics member memory touch [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--id TEXT` | Memory document id. [required] |
| `--team` | Operate on team memory instead of personal memory. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member memory update`

Replace selected body or metadata fields and move the document to the digest front.

```text
guildbotics member memory update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--id TEXT` | Memory document id. [required] |
| `--team` | Operate on team memory instead of personal memory. |
| `--title TEXT` | New document title. |
| `--summary TEXT` | New one-line summary. |
| `--keyword TEXT` | Replace all recall keywords. May be repeated. |
| `--add-keyword TEXT` | Add a recall keyword. May be repeated. |
| `--remove-keyword TEXT` | Remove a recall keyword. May be repeated. |
| `--ticket TEXT` | Related ticket URL (source anchor). May be repeated. |
| `--pr TEXT` | Related PR URL (source anchor). May be repeated. |
| `--channel TEXT` | Related chat channel URL (source anchor). May be repeated. |
| `--thread TEXT` | Related chat thread URL (source anchor). May be repeated. |
| `--pin` | Pin as a standing rule included in member context. |
| `--unpin` | Remove the pin. |
| `--kind [note\|policy]` | Change the document kind; 'policy' requires --policy-approved. |
| `--content-stdin` | Read the entire document body from standard input. |
| `--policy-approved` | Confirm that a human approved this policy memory change. |
| `--set TEXT` | Extra metadata as key=value. May be repeated. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member task`

Workflow task-run completion records.

```text
guildbotics member task [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member task complete`](#guildbotics-member-task-complete) | Finish a ticket workflow run with evidence. |
| [`guildbotics member task status`](#guildbotics-member-task-status) | Inspect recorded run evidence. |

## `guildbotics member task complete`

Finish a ticket workflow run with evidence.

```text
guildbotics member task complete [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--run-id TEXT` | Workflow run id. [required] |
| `--ticket-url TEXT` | Ticket URL the completed run worked on. [required] |
| `--status [done\|asking\|blocked]` | Run outcome. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member task status`

Inspect recorded run evidence.

```text
guildbotics member task status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--run-id TEXT` | Workflow run id. [required] |
| `--person TEXT` | Accepted for consistency with other member commands; not required. [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics run`

Run the GuildBotics application.

```text
guildbotics run [OPTIONS] CUSTOM_COMMAND [COMMAND_ARGS]...
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name to run the custom command as. |
| `--cwd TEXT` | Specify the working directory for the custom command. |
| `--help` | Show this message and exit. |

## `guildbotics secrets`

Manage workspace secrets (API keys and tokens).

```text
guildbotics secrets [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--workspace DIRECTORY` | Workspace root to use instead of the persisted active workspace. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics secrets delete`](#guildbotics-secrets-delete) | Delete a stored secret. |
| [`guildbotics secrets export`](#guildbotics-secrets-export) | Export stored secrets in dotenv format (for moving machines). |
| [`guildbotics secrets import`](#guildbotics-secrets-import) | Import secrets from a dotenv-format file into the workspace store. |
| [`guildbotics secrets list`](#guildbotics-secrets-list) | List the names of the stored secrets. |
| [`guildbotics secrets set`](#guildbotics-secrets-set) | Store a secret value (prompts when VALUE is omitted). |
| [`guildbotics secrets status`](#guildbotics-secrets-status) | Show the secret backend used by this workspace. |

## `guildbotics secrets delete`

Delete a stored secret.

```text
guildbotics secrets delete [OPTIONS] KEY
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets export`

Export stored secrets in dotenv format (for moving machines).

```text
guildbotics secrets export [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--file FILE` | Write to a file (created with owner-only permissions) instead of stdout. |
| `--help` | Show this message and exit. |

## `guildbotics secrets import`

Import secrets from a dotenv-format file into the workspace store.

```text
guildbotics secrets import [OPTIONS] FILE
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets list`

List the names of the stored secrets.

```text
guildbotics secrets list [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets set`

Store a secret value (prompts when VALUE is omitted).

```text
guildbotics secrets set [OPTIONS] KEY [VALUE]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets status`

Show the secret backend used by this workspace.

```text
guildbotics secrets status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics start`

Start GuildBotics runtimes (scheduler and event listener runner).

```text
guildbotics start [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--only [scheduler\|events]` | Start only one runtime instead of both scheduler and event listener runner. |
| `--max-consecutive-errors INTEGER` | Stop a worker after this many consecutive workflow errors. [default: 3] |
| `--help` | Show this message and exit. |

## `guildbotics stop`

Gracefully stop a CLI-managed background service.

```text
guildbotics stop [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--timeout INTEGER` | Seconds to wait at each stop stage [default: 30] |
| `--force` | Cancel in-flight work after timeout, then SIGKILL as a last resort |
| `--help` | Show this message and exit. |

## `guildbotics version`

Print version.

```text
guildbotics version [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics workspace`

Manage the active GuildBotics workspace used by AI CLI tools.

```text
guildbotics workspace [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics workspace current`](#guildbotics-workspace-current) | Show the persisted active workspace. |
| [`guildbotics workspace status`](#guildbotics-workspace-status) | Show active workspace status without failing when it is missing. |
| [`guildbotics workspace use`](#guildbotics-workspace-use) | Persist the active workspace for desktop and external AI CLI tools. |

## `guildbotics workspace current`

Show the persisted active workspace.

```text
guildbotics workspace current [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics workspace status`

Show active workspace status without failing when it is missing.

```text
guildbotics workspace status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics workspace use`

Persist the active workspace for desktop and external AI CLI tools.

```text
guildbotics workspace use [OPTIONS] WORKSPACE_DIR
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |
