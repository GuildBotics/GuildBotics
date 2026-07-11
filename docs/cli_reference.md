# CLI Reference

This file is generated from the Click command definitions in `guildbotics.cli:main`.
Do not edit it directly. Regenerate it with:

```bash
uv run --no-sync python scripts/generate-cli-reference.py
```

## Commands

| Command | Description |
| --- | --- |
| `guildbotics` | GuildBotics CLI entrypoint. |
| `guildbotics kill` | Immediately force kill the running scheduler. Equivalent to: `guildbotics stop --force --timeout 0`. |
| `guildbotics member` | Operate as a configured GuildBotics member. |
| `guildbotics member chat` | Chat identity, posting, replies, reactions, and run completion. |
| `guildbotics member chat complete` | — |
| `guildbotics member chat identity` | — |
| `guildbotics member chat inspect` | Inspect Slack channel or thread messages for interactive decisions. |
| `guildbotics member chat inspect channel` | — |
| `guildbotics member chat inspect thread` | — |
| `guildbotics member chat noop` | — |
| `guildbotics member chat post` | — |
| `guildbotics member chat reaction` | Chat reaction operations. |
| `guildbotics member chat reaction add` | — |
| `guildbotics member chat reply` | — |
| `guildbotics member context` | Show non-secret member context. |
| `guildbotics member git` | Prepare, commit, push, and publish member git workspaces. |
| `guildbotics member git commit` | Commit already-staged changes with the member identity. Stage the files you want with plain git (e.g. ``git add``) first; this command commits only what is staged and applies the member name/email to that single commit without changing the repository's git config. |
| `guildbotics member git prepare` | — |
| `guildbotics member git publish` | Commit already-staged changes with the member identity, then push. Stage the files you want with plain git (e.g. ``git add``) first; this commits only what is staged with the member name/email and pushes the branch using the member credential. |
| `guildbotics member git push` | — |
| `guildbotics member github` | GitHub issue, pull request, and reaction capabilities. |
| `guildbotics member github issue` | GitHub issue operations. |
| `guildbotics member github issue comment` | — |
| `guildbotics member github issue create` | — |
| `guildbotics member github issue inspect` | — |
| `guildbotics member github pr` | GitHub pull request operations. |
| `guildbotics member github pr comment` | — |
| `guildbotics member github pr create` | — |
| `guildbotics member github pr inspect` | — |
| `guildbotics member github pr reply` | — |
| `guildbotics member github pr review-comment` | — |
| `guildbotics member github reaction` | GitHub reaction operations. |
| `guildbotics member github reaction add` | — |
| `guildbotics member help` | Print the member capability reference (commands and cross-cutting rules). This is the same reference embedded in ``member context``; use it to reread the available commands without re-running the full context. |
| `guildbotics member memory` | Record, recall, and maintain member memory documents. |
| `guildbotics member memory archive` | — |
| `guildbotics member memory get` | — |
| `guildbotics member memory promote` | — |
| `guildbotics member memory recall` | — |
| `guildbotics member memory record` | — |
| `guildbotics member memory touch` | — |
| `guildbotics member memory update` | — |
| `guildbotics member task` | Workflow task-run completion records. |
| `guildbotics member task complete` | — |
| `guildbotics member task status` | — |
| `guildbotics run` | Run the GuildBotics application. |
| `guildbotics secrets` | Manage workspace secrets (API keys and tokens). |
| `guildbotics secrets delete` | Delete a stored secret. |
| `guildbotics secrets export` | Export stored secrets in dotenv format (for moving machines). |
| `guildbotics secrets import` | Import secrets from a dotenv-format file into the workspace store. |
| `guildbotics secrets list` | List the names of the stored secrets. |
| `guildbotics secrets migrate` | Move secrets from the .env file into the OS keychain. |
| `guildbotics secrets set` | Store a secret value (prompts when VALUE is omitted). |
| `guildbotics secrets status` | Show the secret backend used by this workspace. |
| `guildbotics start` | Start GuildBotics runtimes (scheduler and event listener runner). |
| `guildbotics stop` | Gracefully stop the running scheduler process. |
| `guildbotics version` | Print version. |
| `guildbotics workspace` | Manage the active GuildBotics workspace used by AI CLI tools. |
| `guildbotics workspace current` | Show the persisted active workspace. |
| `guildbotics workspace status` | Show active workspace status without failing when it is missing. |
| `guildbotics workspace use` | Persist the active workspace for desktop and external AI CLI tools. |

## `guildbotics`

GuildBotics CLI entrypoint.

```text
Usage: guildbotics [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--version` | option | boolean | no | false | Show the version and exit. |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics kill`

Immediately force kill the running scheduler. Equivalent to: `guildbotics stop --force --timeout 0`.

```text
Usage: guildbotics kill [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics member`

Operate as a configured GuildBotics member.

```text
Usage: guildbotics member [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--workspace` | option | directory | no | — | Workspace root to use instead of the persisted active workspace. |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member chat`

Chat identity, posting, replies, reactions, and run completion.

```text
Usage: guildbotics member chat [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat complete`

—

```text
Usage: guildbotics member chat complete [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--run-id` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | yes | — | — |
| `--thread-ts` | option | text | yes | — | — |
| `--event-id` | option | text | yes | — | — |
| `--status` | option | done \| asking \| blocked | yes | — | — |
| `--summary-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat identity`

—

```text
Usage: guildbotics member chat identity [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat inspect`

Inspect Slack channel or thread messages for interactive decisions.

```text
Usage: guildbotics member chat inspect [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member chat inspect channel`

—

```text
Usage: guildbotics member chat inspect channel [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | no |  | — |
| `--channel-name` | option | text | no |  | — |
| `--oldest-ts` | option | text | no |  | — |
| `--latest-ts` | option | text | no |  | — |
| `--limit` | option | integer range (1..200) | no | 50 | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member chat inspect thread`

—

```text
Usage: guildbotics member chat inspect thread [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | no |  | — |
| `--channel-name` | option | text | no |  | — |
| `--thread-ts` | option | text | no |  | — |
| `--message-url` | option | text | no |  | — |
| `--limit` | option | integer range (1..200) | no | 100 | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat noop`

—

```text
Usage: guildbotics member chat noop [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--run-id` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | yes | — | — |
| `--thread-ts` | option | text | yes | — | — |
| `--event-id` | option | text | yes | — | — |
| `--reason-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat post`

—

```text
Usage: guildbotics member chat post [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | no |  | — |
| `--channel-name` | option | text | no |  | — |
| `--body-file` | option | path | no | — | — |
| `--body-stdin` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat reaction`

Chat reaction operations.

```text
Usage: guildbotics member chat reaction [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member chat reaction add`

—

```text
Usage: guildbotics member chat reaction add [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | no |  | — |
| `--channel-name` | option | text | no |  | — |
| `--message-ts` | option | text | no |  | — |
| `--message-url` | option | text | no |  | — |
| `--reaction` | option | ack \| agree \| celebrate \| support | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member chat reply`

—

```text
Usage: guildbotics member chat reply [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--service` | option | slack | no | slack | — |
| `--channel-id` | option | text | no |  | — |
| `--channel-name` | option | text | no |  | — |
| `--thread-ts` | option | text | no |  | — |
| `--message-url` | option | text | no |  | — |
| `--body-file` | option | path | no | — | — |
| `--body-stdin` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member context`

Show non-secret member context.

```text
Usage: guildbotics member context [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | Person ID or name. |
| `--check-credentials` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member git`

Prepare, commit, push, and publish member git workspaces.

```text
Usage: guildbotics member git [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member git commit`

Commit already-staged changes with the member identity. Stage the files you want with plain git (e.g. ``git add``) first; this command commits only what is staged and applies the member name/email to that single commit without changing the repository's git config.

```text
Usage: guildbotics member git commit [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo-path` | option | path | yes | — | — |
| `--message-file` | option | path | no | — | — |
| `--message-stdin` | option | boolean | no | false | Read the commit message from standard input instead of a file. |
| `--workspace-mode` | option | member \| current | no | member | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member git prepare`

—

```text
Usage: guildbotics member git prepare [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--issue-url` | option | text | no |  | — |
| `--pr-url` | option | text | no |  | — |
| `--repo` | option | text | no |  | Target repository as <owner>/<repo>. |
| `--branch` | option | text | no |  | Branch to create or check out (with --repo). |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member git publish`

Commit already-staged changes with the member identity, then push. Stage the files you want with plain git (e.g. ``git add``) first; this commits only what is staged with the member name/email and pushes the branch using the member credential.

```text
Usage: guildbotics member git publish [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo-path` | option | path | yes | — | — |
| `--message-file` | option | path | no | — | — |
| `--message-stdin` | option | boolean | no | false | Read the commit message from standard input instead of a file. |
| `--workspace-mode` | option | member \| current | no | member | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member git push`

—

```text
Usage: guildbotics member git push [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo-path` | option | path | yes | — | — |
| `--workspace-mode` | option | member \| current | no | member | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member github`

GitHub issue, pull request, and reaction capabilities.

```text
Usage: guildbotics member github [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member github issue`

GitHub issue operations.

```text
Usage: guildbotics member github issue [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github issue comment`

—

```text
Usage: guildbotics member github issue comment [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--body-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github issue create`

—

```text
Usage: guildbotics member github issue create [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo` | option | text | yes | — | — |
| `--title-file` | option | path | yes | — | — |
| `--body-file` | option | path | yes | — | — |
| `--add-to-project, --no-add-to-project` | option | boolean | no | true | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github issue inspect`

—

```text
Usage: guildbotics member github issue inspect [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member github pr`

GitHub pull request operations.

```text
Usage: guildbotics member github pr [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github pr comment`

—

```text
Usage: guildbotics member github pr comment [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--body-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github pr create`

—

```text
Usage: guildbotics member github pr create [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo` | option | text | yes | — | — |
| `--head` | option | text | yes | — | — |
| `--base` | option | text | no |  | Base branch for the pull request. Defaults to the repository default branch. |
| `--title-file` | option | path | no | — | — |
| `--body-file` | option | path | no | — | — |
| `--content-stdin` | option | boolean | no | false | Read PR title and body from standard input. The first line is the title; the remaining content is the body. |
| `--issue-url` | option | text | no |  | — |
| `--draft` | option | auto \| true \| false | no | auto | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github pr inspect`

—

```text
Usage: guildbotics member github pr inspect [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--include-comments` | option | boolean | no | false | — |
| `--include-diff` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github pr reply`

—

```text
Usage: guildbotics member github pr reply [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--reply-target-id` | option | integer | yes | — | — |
| `--body-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github pr review-comment`

—

```text
Usage: guildbotics member github pr review-comment [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--url` | option | text | yes | — | — |
| `--path` | option | text | yes | — | — |
| `--line` | option | integer range (1..…) | yes | — | — |
| `--side` | option | LEFT \| RIGHT | no | RIGHT | — |
| `--start-line` | option | integer range (1..…) | no | — | — |
| `--start-side` | option | LEFT \| RIGHT | no | — | — |
| `--body-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member github reaction`

GitHub reaction operations.

```text
Usage: guildbotics member github reaction [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

###### `guildbotics member github reaction add`

—

```text
Usage: guildbotics member github reaction add [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--repo` | option | text | yes | — | — |
| `--target` | option | issue-comment \| pr-review-comment | yes | — | — |
| `--comment-id` | option | integer | yes | — | — |
| `--reaction` | option | +1 \| eyes \| heart \| hooray \| rocket \| laugh \| confused \| -1 | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member help`

Print the member capability reference (commands and cross-cutting rules). This is the same reference embedded in ``member context``; use it to reread the available commands without re-running the full context.

```text
Usage: guildbotics member help [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member memory`

Record, recall, and maintain member memory documents.

```text
Usage: guildbotics member memory [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory archive`

—

```text
Usage: guildbotics member memory archive [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--id` | option | text | yes | — | — |
| `--team` | option | boolean | no | false | — |
| `--policy-approved` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory get`

—

```text
Usage: guildbotics member memory get [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--id` | option | text | yes | — | — |
| `--team` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory promote`

—

```text
Usage: guildbotics member memory promote [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--id` | option | text | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory recall`

—

```text
Usage: guildbotics member memory recall [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--query` | option | text, repeatable | no | — | — |
| `--meta-only` | option | boolean | no | false | — |
| `--limit` | option | integer range (1..200) | no | 20 | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory record`

—

```text
Usage: guildbotics member memory record [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--scope` | option | personal \| team | no | personal | — |
| `--title` | option | text | yes | — | — |
| `--summary` | option | text | no |  | — |
| `--keyword` | option | text, repeatable | no | — | — |
| `--ticket` | option | text, repeatable | no | — | — |
| `--pr` | option | text, repeatable | no | — | — |
| `--channel` | option | text, repeatable | no | — | — |
| `--thread` | option | text, repeatable | no | — | — |
| `--kind` | option | note \| policy | no | note | — |
| `--pin` | option | boolean | no | false | — |
| `--body-file` | option | file | no | — | — |
| `--body-stdin` | option | boolean | no | false | — |
| `--policy-approved` | option | boolean | no | false | — |
| `--set` | option | text, repeatable | no | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory touch`

—

```text
Usage: guildbotics member memory touch [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--id` | option | text | yes | — | — |
| `--team` | option | boolean | no | false | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member memory update`

—

```text
Usage: guildbotics member memory update [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--id` | option | text | yes | — | — |
| `--team` | option | boolean | no | false | — |
| `--title` | option | text | no | — | — |
| `--summary` | option | text | no | — | — |
| `--keyword` | option | text, repeatable | no | — | — |
| `--add-keyword` | option | text, repeatable | no | — | — |
| `--remove-keyword` | option | text, repeatable | no | — | — |
| `--ticket` | option | text, repeatable | no | — | — |
| `--pr` | option | text, repeatable | no | — | — |
| `--channel` | option | text, repeatable | no | — | — |
| `--thread` | option | text, repeatable | no | — | — |
| `--pin` | option | boolean | no | — | — |
| `--unpin` | option | boolean | no | false | — |
| `--kind` | option | note \| policy | no | — | — |
| `--body-file` | option | file | no | — | — |
| `--body-stdin` | option | boolean | no | false | — |
| `--policy-approved` | option | boolean | no | false | — |
| `--set` | option | text, repeatable | no | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics member task`

Workflow task-run completion records.

```text
Usage: guildbotics member task [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member task complete`

—

```text
Usage: guildbotics member task complete [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | yes | — | — |
| `--run-id` | option | text | yes | — | — |
| `--ticket-url` | option | text | yes | — | — |
| `--status` | option | done \| asking \| blocked | yes | — | — |
| `--summary-file` | option | path | yes | — | — |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

##### `guildbotics member task status`

—

```text
Usage: guildbotics member task status [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--run-id` | option | text | yes | — | — |
| `--person` | option | text | no |  | Accepted for consistency with other member commands; not required. |
| `--format` | option | json \| markdown | no | json | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics run`

Run the GuildBotics application.

```text
Usage: guildbotics run [OPTIONS] CUSTOM_COMMAND [COMMAND_ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--person` | option | text | no | — | Person ID or name to run the custom command as. |
| `--cwd` | option | text | no | — | Specify the working directory for the custom command. |
| `CUSTOM_COMMAND` | argument | text | yes | — | — |
| `COMMAND_ARGS...` | argument | text | no | — | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics secrets`

Manage workspace secrets (API keys and tokens).

```text
Usage: guildbotics secrets [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--workspace` | option | directory | no | — | Workspace root to use instead of the persisted active workspace. |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets delete`

Delete a stored secret.

```text
Usage: guildbotics secrets delete [OPTIONS] KEY
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `KEY` | argument | text | yes | — | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets export`

Export stored secrets in dotenv format (for moving machines).

```text
Usage: guildbotics secrets export [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--file` | option | file | no | — | Write to a file (created with owner-only permissions) instead of stdout. |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets import`

Import secrets from a dotenv-format file into the workspace store.

```text
Usage: guildbotics secrets import [OPTIONS] FILE
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `FILE` | argument | file | yes | — | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets list`

List the names of the stored secrets.

```text
Usage: guildbotics secrets list [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets migrate`

Move secrets from the .env file into the OS keychain.

```text
Usage: guildbotics secrets migrate [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--key` | option | text, repeatable | no | — | Additional .env key to migrate (repeatable). |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets set`

Store a secret value (prompts when VALUE is omitted).

```text
Usage: guildbotics secrets set [OPTIONS] KEY [VALUE]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `KEY` | argument | text | yes | — | — |
| `VALUE` | argument | text | no | — | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics secrets status`

Show the secret backend used by this workspace.

```text
Usage: guildbotics secrets status [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics start`

Start GuildBotics runtimes (scheduler and event listener runner).

```text
Usage: guildbotics start [OPTIONS] [DEFAULT_ROUTINE_COMMANDS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--only` | option | scheduler \| events | no | — | Start only one runtime instead of both scheduler and event listener runner. |
| `--max-consecutive-errors` | option | integer | no | 3 | Stop a worker after this many consecutive workflow errors. |
| `DEFAULT_ROUTINE_COMMANDS...` | argument | text | no | — | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics stop`

Gracefully stop the running scheduler process.

```text
Usage: guildbotics stop [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--timeout` | option | integer | no | 30 | Seconds to wait at each stop stage |
| `--force` | option | boolean | no | false | Cancel in-flight work after timeout, then SIGKILL as a last resort |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics version`

Print version.

```text
Usage: guildbotics version [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

### `guildbotics workspace`

Manage the active GuildBotics workspace used by AI CLI tools.

```text
Usage: guildbotics workspace [OPTIONS] COMMAND [ARGS]...
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics workspace current`

Show the persisted active workspace.

```text
Usage: guildbotics workspace current [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics workspace status`

Show active workspace status without failing when it is missing.

```text
Usage: guildbotics workspace status [OPTIONS]
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |

#### `guildbotics workspace use`

Persist the active workspace for desktop and external AI CLI tools.

```text
Usage: guildbotics workspace use [OPTIONS] WORKSPACE_DIR
```

| Parameter | Kind | Type | Required | Default | Description |
| --- | --- | --- | --- | --- | --- |
| `WORKSPACE_DIR` | argument | directory | yes | — | — |
| `--format` | option | json \| markdown | no | markdown | — |
| `--help` | option | boolean | no | false | Show this message and exit. |
