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
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill the running scheduler. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
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
| [`guildbotics member github pr`](#guildbotics-member-github-pr) | GitHub pull request operations. |
| [`guildbotics member github pr comment`](#guildbotics-member-github-pr-comment) | Comment on a PR conversation. |
| [`guildbotics member github pr create`](#guildbotics-member-github-pr-create) | Open or reuse a PR. |
| [`guildbotics member github pr inspect`](#guildbotics-member-github-pr-inspect) | Read a PR, optionally including review threads and diff comment coordinates. |
| [`guildbotics member github pr reply`](#guildbotics-member-github-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member github pr review-comment`](#guildbotics-member-github-pr-review-comment) | Create a new inline review comment on a PR diff line. |
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
| [`guildbotics secrets migrate`](#guildbotics-secrets-migrate) | Move secrets from the .env file into the OS keychain. |
| [`guildbotics secrets set`](#guildbotics-secrets-set) | Store a secret value (prompts when VALUE is omitted). |
| [`guildbotics secrets status`](#guildbotics-secrets-status) | Show the secret backend used by this workspace. |
| [`guildbotics start`](#guildbotics-start) | Start GuildBotics runtimes (scheduler and event listener runner). |
| [`guildbotics stop`](#guildbotics-stop) | Gracefully stop the running scheduler process. |
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
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill the running scheduler. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
| [`guildbotics run`](#guildbotics-run) | Run the GuildBotics application. |
| [`guildbotics secrets`](#guildbotics-secrets) | Manage workspace secrets (API keys and tokens). |
| [`guildbotics start`](#guildbotics-start) | Start GuildBotics runtimes (scheduler and event listener runner). |
| [`guildbotics stop`](#guildbotics-stop) | Gracefully stop the running scheduler process. |
| [`guildbotics version`](#guildbotics-version) | Print version. |
| [`guildbotics workspace`](#guildbotics-workspace) | Manage the active GuildBotics workspace used by AI CLI tools. |

## `guildbotics kill`

Immediately force kill the running scheduler.

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
| [`guildbotics member chat`](#guildbotics-member-chat) | Chat identity, posting, replies, reactions, and run completion. |
| [`guildbotics member context`](#guildbotics-member-context) | Show non-secret member context. |
| [`guildbotics member git`](#guildbotics-member-git) | Prepare, commit, push, and publish member git workspaces. |
| [`guildbotics member github`](#guildbotics-member-github) | GitHub issue, pull request, and reaction capabilities. |
| [`guildbotics member help`](#guildbotics-member-help) | Print the member capability reference (commands and cross-cutting rules). |
| [`guildbotics member memory`](#guildbotics-member-memory) | Record, recall, and maintain member memory documents. |
| [`guildbotics member task`](#guildbotics-member-task) | Workflow task-run completion records. |

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
| `--person TEXT` | [required] |
| `--run-id TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | [required] |
| `--thread-ts TEXT` | [required] |
| `--event-id TEXT` | [required] |
| `--status [done\|asking\|blocked]` | [required] |
| `--summary-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member chat identity`

Show the member's chat identity.

```text
guildbotics member chat identity [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | - |
| `--channel-name TEXT` | - |
| `--oldest-ts TEXT` | - |
| `--latest-ts TEXT` | - |
| `--limit INTEGER RANGE` | [1\<=x\<=200] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member chat inspect thread`

Read a thread before replying or reacting.

```text
guildbotics member chat inspect thread [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | - |
| `--channel-name TEXT` | - |
| `--thread-ts TEXT` | - |
| `--message-url TEXT` | - |
| `--limit INTEGER RANGE` | [1\<=x\<=200] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member chat noop`

Record a deliberate no-op for a chat event.

```text
guildbotics member chat noop [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--run-id TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | [required] |
| `--thread-ts TEXT` | [required] |
| `--event-id TEXT` | [required] |
| `--reason-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member chat post`

Post a new channel message.

```text
guildbotics member chat post [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | - |
| `--channel-name TEXT` | - |
| `--body-file PATH` | - |
| `--body-stdin` | - |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | - |
| `--channel-name TEXT` | - |
| `--message-ts TEXT` | - |
| `--message-url TEXT` | - |
| `--reaction [ack\|agree\|celebrate\|support]` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member chat reply`

Reply in a thread in the member voice.

```text
guildbotics member chat reply [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--service [slack]` | - |
| `--channel-id TEXT` | - |
| `--channel-name TEXT` | - |
| `--thread-ts TEXT` | - |
| `--message-url TEXT` | - |
| `--body-file PATH` | - |
| `--body-stdin` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member context`

Show non-secret member context.

```text
guildbotics member context [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name. [required] |
| `--check-credentials` | - |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--repo-path PATH` | [required] |
| `--message-file PATH` | - |
| `--message-stdin` | Read the commit message from standard input instead of a file. |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member git prepare`

Clone/checkout an isolated member workspace: a ticket branch (--issue-url), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo --branch). --repo cannot be combined with the URL options.

```text
guildbotics member git prepare [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--issue-url TEXT` | - |
| `--pr-url TEXT` | - |
| `--repo TEXT` | Target repository as \<owner\>/\<repo\>. |
| `--branch TEXT` | Branch to create or check out (with --repo). |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--repo-path PATH` | [required] |
| `--message-file PATH` | - |
| `--message-stdin` | Read the commit message from standard input instead of a file. |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member git push`

Push the current branch with the member credential.

```text
guildbotics member git push [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--repo-path PATH` | [required] |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format [json\|markdown]` | - |
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

## `guildbotics member github issue comment`

Comment on an issue in the member voice.

```text
guildbotics member github issue comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--body-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github issue create`

Open a follow-up issue.

```text
guildbotics member github issue create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--repo TEXT` | [required] |
| `--title-file PATH` | [required] |
| `--body-file PATH` | [required] |
| `--add-to-project / --no-add-to-project` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github issue inspect`

Read an issue and its comments.

```text
guildbotics member github issue inspect [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--format [json\|markdown]` | - |
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
| [`guildbotics member github pr create`](#guildbotics-member-github-pr-create) | Open or reuse a PR. |
| [`guildbotics member github pr inspect`](#guildbotics-member-github-pr-inspect) | Read a PR, optionally including review threads and diff comment coordinates. |
| [`guildbotics member github pr reply`](#guildbotics-member-github-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member github pr review-comment`](#guildbotics-member-github-pr-review-comment) | Create a new inline review comment on a PR diff line. |

## `guildbotics member github pr comment`

Comment on a PR conversation.

```text
guildbotics member github pr comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--body-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github pr create`

Open or reuse a PR.

```text
guildbotics member github pr create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--repo TEXT` | [required] |
| `--head TEXT` | [required] |
| `--base TEXT` | Base branch for the pull request. Defaults to the repository default branch. |
| `--title-file PATH` | - |
| `--body-file PATH` | - |
| `--content-stdin` | Read PR title and body from standard input. The first line is the title; the remaining content is the body. |
| `--issue-url TEXT` | - |
| `--draft [auto\|true\|false]` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github pr inspect`

Read a PR, optionally including review threads and diff comment coordinates.

```text
guildbotics member github pr inspect [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--include-comments` | - |
| `--include-diff` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github pr reply`

Reply to an inline review thread.

```text
guildbotics member github pr reply [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--reply-target-id INTEGER` | [required] |
| `--body-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member github pr review-comment`

Create a new inline review comment on a PR diff line.

```text
guildbotics member github pr review-comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--url TEXT` | [required] |
| `--path TEXT` | [required] |
| `--line INTEGER RANGE` | [x\>=1; required] |
| `--side [LEFT\|RIGHT]` | - |
| `--start-line INTEGER RANGE` | [x\>=1] |
| `--start-side [LEFT\|RIGHT]` | - |
| `--body-file PATH` | [required] |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--repo TEXT` | [required] |
| `--target [issue-comment\|pr-review-comment]` | [required] |
| `--comment-id INTEGER` | [required] |
| `--reaction [+1\|eyes\|heart\|hooray\|rocket\|laugh\|confused\|-1]` | [required] |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--id TEXT` | [required] |
| `--team` | - |
| `--policy-approved` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory get`

Read one memory document's metadata, body, and asset paths without changing recency.

```text
guildbotics member memory get [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--id TEXT` | [required] |
| `--team` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory promote`

Move a personal memory into team memory without changing the document id.

```text
guildbotics member memory promote [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--id TEXT` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory recall`

Search personal and team memory by literal OR queries and return compact hits.

```text
guildbotics member memory recall [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--query TEXT` | - |
| `--meta-only` | - |
| `--limit INTEGER RANGE` | [1\<=x\<=200] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory record`

Create a memory document and move it to the front of the digest.

```text
guildbotics member memory record [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--scope [personal\|team]` | [default: personal] |
| `--title TEXT` | [required] |
| `--summary TEXT` | - |
| `--keyword TEXT` | - |
| `--ticket TEXT` | - |
| `--pr TEXT` | - |
| `--channel TEXT` | - |
| `--thread TEXT` | - |
| `--kind [note\|policy]` | - |
| `--pin` | - |
| `--body-file FILE` | - |
| `--body-stdin` | - |
| `--policy-approved` | - |
| `--set TEXT` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory touch`

Mark a useful memory as actually used by moving it to the digest front.

```text
guildbotics member memory touch [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--id TEXT` | [required] |
| `--team` | - |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member memory update`

Replace selected body or metadata fields and move the document to the digest front.

```text
guildbotics member memory update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | [required] |
| `--id TEXT` | [required] |
| `--team` | - |
| `--title TEXT` | - |
| `--summary TEXT` | - |
| `--keyword TEXT` | - |
| `--add-keyword TEXT` | - |
| `--remove-keyword TEXT` | - |
| `--ticket TEXT` | - |
| `--pr TEXT` | - |
| `--channel TEXT` | - |
| `--thread TEXT` | - |
| `--pin` | - |
| `--unpin` | - |
| `--kind [note\|policy]` | - |
| `--body-file FILE` | - |
| `--body-stdin` | - |
| `--policy-approved` | - |
| `--set TEXT` | - |
| `--format [json\|markdown]` | - |
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
| `--person TEXT` | [required] |
| `--run-id TEXT` | [required] |
| `--ticket-url TEXT` | [required] |
| `--status [done\|asking\|blocked]` | [required] |
| `--summary-file PATH` | [required] |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics member task status`

Inspect recorded run evidence.

```text
guildbotics member task status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--run-id TEXT` | [required] |
| `--person TEXT` | Accepted for consistency with other member commands; not required. |
| `--format [json\|markdown]` | - |
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
| [`guildbotics secrets migrate`](#guildbotics-secrets-migrate) | Move secrets from the .env file into the OS keychain. |
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

## `guildbotics secrets migrate`

Move secrets from the .env file into the OS keychain.

```text
guildbotics secrets migrate [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--key TEXT` | Additional .env key to migrate (repeatable). |
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
guildbotics start [OPTIONS] [DEFAULT_ROUTINE_COMMANDS]...
```

| Option | Description |
| --- | --- |
| `--only [scheduler\|events]` | Start only one runtime instead of both scheduler and event listener runner. |
| `--max-consecutive-errors INTEGER` | Stop a worker after this many consecutive workflow errors. [default: 3] |
| `--help` | Show this message and exit. |

## `guildbotics stop`

Gracefully stop the running scheduler process.

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
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics workspace status`

Show active workspace status without failing when it is missing.

```text
guildbotics workspace status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |

## `guildbotics workspace use`

Persist the active workspace for desktop and external AI CLI tools.

```text
guildbotics workspace use [OPTIONS] WORKSPACE_DIR
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | - |
| `--help` | Show this message and exit. |
