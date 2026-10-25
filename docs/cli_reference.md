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
| [`guildbotics diagnostics`](#guildbotics-diagnostics) | Read recorded diagnostics for a workspace (read-only). |
| [`guildbotics diagnostics system`](#guildbotics-diagnostics-system) | Show service-wide records that belong to no single execution. |
| [`guildbotics diagnostics trace`](#guildbotics-diagnostics-trace) | Show one execution's summary and records. |
| [`guildbotics diagnostics traces`](#guildbotics-diagnostics-traces) | List recorded executions, newest first. |
| [`guildbotics environment`](#guildbotics-environment) | Build and log in to the isolated agent environment on this device. |
| [`guildbotics environment build`](#guildbotics-environment-build) | Build the environment the shared declaration asks for. |
| [`guildbotics environment image`](#guildbotics-environment-image) | The base images this device holds for the environment. |
| [`guildbotics environment image declare`](#guildbotics-environment-image-declare) | Name the base image in the workspace's shared declaration. |
| [`guildbotics environment image list`](#guildbotics-environment-image-list) | List the images this device's declaration may name as its base. |
| [`guildbotics environment image load`](#guildbotics-environment-image-load) | Load an image archive into this device's runtime. |
| [`guildbotics environment image load-ci`](#guildbotics-environment-image-load-ci) | Load an image archive into the CI runner of the workspace's code host. |
| [`guildbotics environment login`](#guildbotics-environment-login) | Log in to an AI CLI tool inside the environment. |
| [`guildbotics environment remove`](#guildbotics-environment-remove) | Remove the workspace's snapshots from this device (logins are kept). |
| [`guildbotics environment status`](#guildbotics-environment-status) | Show the environment's runtime, snapshot, and logins on this device. |
| [`guildbotics git-credential`](#guildbotics-git-credential) | Hand git the human member's credential for a remote of a service GuildBotics is connected to on this machine (ACTION is git's: get, store, or erase). |
| [`guildbotics hub`](#guildbotics-hub) | Host a synchronization hub for GuildBotics workspaces on this machine. |
| [`guildbotics hub create`](#guildbotics-hub-create) | Make this machine a hub, or show the hub it already hosts. |
| [`guildbotics hub identity`](#guildbotics-hub-identity) | Manage the repository of the members' identities, which every workspace shares. |
| [`guildbotics hub identity claim`](#guildbotics-hub-identity-claim) | Show the identities' repository, registering IDENTITY_ID if there is none. |
| [`guildbotics hub live`](#guildbotics-hub-live) | Publish and watch transient workspace live state. |
| [`guildbotics hub live publish`](#guildbotics-hub-live-publish) | Replace one live snapshot for each non-empty stdin line. |
| [`guildbotics hub live watch`](#guildbotics-hub-live-watch) | Poll live snapshots and head updates until the SSH connection closes. |
| [`guildbotics hub owner`](#guildbotics-hub-owner) | Inspect or explicitly change a workspace's service owner. |
| [`guildbotics hub owner claim`](#guildbotics-hub-owner-claim) | Claim ownership only when the workspace has no owner. |
| [`guildbotics hub owner get`](#guildbotics-hub-owner-get) | Show the current service owner, if one has been claimed. |
| [`guildbotics hub owner transfer`](#guildbotics-hub-owner-transfer) | Move ownership explicitly to another device. |
| [`guildbotics hub secret`](#guildbotics-hub-secret) | Hold secret values for the devices that share a workspace. |
| [`guildbotics hub secret list`](#guildbotics-hub-secret-list) | Report which generation this hub holds for each key, without values. |
| [`guildbotics hub secret receive`](#guildbotics-hub-secret-receive) | Store the values a device sends, one framed entry each. |
| [`guildbotics hub secret send`](#guildbotics-hub-secret-send) | Write the requested values back to the device, one framed entry each. |
| [`guildbotics hub status`](#guildbotics-hub-status) | Show whether this machine hosts a hub, and which workspaces it holds. |
| [`guildbotics hub workspace`](#guildbotics-hub-workspace) | Manage the repositories this hub holds, one per workspace. |
| [`guildbotics hub workspace create`](#guildbotics-hub-workspace-create) | Create the repository a workspace synchronizes through. |
| [`guildbotics hub workspace list`](#guildbotics-hub-workspace-list) | List the workspaces this hub holds. |
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill a CLI-managed background service. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
| [`guildbotics member chat`](#guildbotics-member-chat) | Chat identity, posting, replies, reactions, and run completion. |
| [`guildbotics member chat complete`](#guildbotics-member-chat-complete) | Finish the chat workflow run on its event with evidence. |
| [`guildbotics member chat identity`](#guildbotics-member-chat-identity) | Show the member's chat identity. |
| [`guildbotics member chat inspect`](#guildbotics-member-chat-inspect) | Inspect chat channel or thread messages for interactive decisions. |
| [`guildbotics member chat inspect channel`](#guildbotics-member-chat-inspect-channel) | Read recent channel messages. |
| [`guildbotics member chat inspect thread`](#guildbotics-member-chat-inspect-thread) | Read a thread before replying or reacting. |
| [`guildbotics member chat noop`](#guildbotics-member-chat-noop) | Record a deliberate no-op for the chat run's event. |
| [`guildbotics member chat post`](#guildbotics-member-chat-post) | Post a new channel message. |
| [`guildbotics member chat reaction`](#guildbotics-member-chat-reaction) | Chat reaction operations. |
| [`guildbotics member chat reaction add`](#guildbotics-member-chat-reaction-add) | Add a semantic reaction. |
| [`guildbotics member chat reply`](#guildbotics-member-chat-reply) | Reply in a thread in the member voice. |
| [`guildbotics member chat resolve-channel`](#guildbotics-member-chat-resolve-channel) | Resolve a channel name to its channel id without reading or posting; the id is empty when no channel has that name. |
| [`guildbotics member chat updates`](#guildbotics-member-chat-updates) | Check the source thread's durable queue before replying or publishing in a chat workflow; reconsider new messages, and stop external writes when reception is unavailable. |
| [`guildbotics member checks`](#guildbotics-member-checks) | End a run to await the CI checks of its pull requests. |
| [`guildbotics member checks await`](#guildbotics-member-checks-await) | End any run -- a ticket's, a chat thread's, or a command's -- to await its own PRs' pending checks: a notice on each PR, from which the patrol resumes the run's work once they settle. |
| [`guildbotics member context`](#guildbotics-member-context) | Show non-secret member context. |
| [`guildbotics member git`](#guildbotics-member-git) | Prepare, commit, push, and publish member git workspaces. |
| [`guildbotics member git commit`](#guildbotics-member-git-commit) | Commit already-staged changes with the member identity. |
| [`guildbotics member git prepare`](#guildbotics-member-git-prepare) | Clone/checkout an isolated member workspace: a ticket branch `ticket/\<key\>` (--issue-url, with --repo for a ticket that belongs to no repository), a PR head (--pr-url, alone or together with... |
| [`guildbotics member git publish`](#guildbotics-member-git-publish) | Commit already-staged changes with the member identity, then push. |
| [`guildbotics member git push`](#guildbotics-member-git-push) | Push the current branch with the member credential and report readiness for matching open PRs. |
| [`guildbotics member help`](#guildbotics-member-help) | Print the member capability reference (commands and cross-cutting rules). |
| [`guildbotics member memory`](#guildbotics-member-memory) | Record, recall, and maintain member memory documents. |
| [`guildbotics member memory archive`](#guildbotics-member-memory-archive) | Move a stale memory under archived/ and remove it from recall and digest. |
| [`guildbotics member memory get`](#guildbotics-member-memory-get) | Read one memory document's metadata and body without changing recency. |
| [`guildbotics member memory promote`](#guildbotics-member-memory-promote) | Move a personal memory into team memory without changing the document id. |
| [`guildbotics member memory recall`](#guildbotics-member-memory-recall) | Search personal and team memory by literal OR queries and return compact hits. |
| [`guildbotics member memory record`](#guildbotics-member-memory-record) | Create a memory document and move it to the front of the digest. |
| [`guildbotics member memory touch`](#guildbotics-member-memory-touch) | Mark a useful memory as actually used by moving it to the digest front. |
| [`guildbotics member memory update`](#guildbotics-member-memory-update) | Replace selected body or metadata fields and move the document to the digest front. |
| [`guildbotics member pr`](#guildbotics-member-pr) | Read, open, update, comment on, and review pull requests, react to their comments and reviews, and download their CI artifacts. |
| [`guildbotics member pr artifact`](#guildbotics-member-pr-artifact) | CI artifact operations. |
| [`guildbotics member pr artifact download`](#guildbotics-member-pr-artifact-download) | Download and extract a size-limited CI artifact of the PR's head; remove repository files after inspection. |
| [`guildbotics member pr comment`](#guildbotics-member-pr-comment) | Comment on a PR conversation. |
| [`guildbotics member pr create`](#guildbotics-member-pr-create) | Open a PR, or return the existing open PR for the same head and base branches. |
| [`guildbotics member pr reaction`](#guildbotics-member-pr-reaction) | Reactions to pull request comments and reviews. |
| [`guildbotics member pr reaction add`](#guildbotics-member-pr-reaction-add) | React to a conversation comment, a review thread comment, or a review's body of the PR, by the id its read gave it. |
| [`guildbotics member pr read`](#guildbotics-member-pr-read) | Read one bounded page of a PR's resource: pull_requests, pull_request_comments (conversation comments), pull_request_reviews, pull_request_files, pull_request_threads, review_thread_comments, or... |
| [`guildbotics member pr reply`](#guildbotics-member-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member pr review`](#guildbotics-member-pr-review) | Submit a review verdict as a review on the head the member read (`head_sha` of the PR's read); a conversation comment does not consume a review request or make the member a reviewer. |
| [`guildbotics member pr review-comment`](#guildbotics-member-pr-review-comment) | Create a new inline review comment on a diff line of the head the member read; when the head has moved, read the new commits and comment again. |
| [`guildbotics member pr update`](#guildbotics-member-pr-update) | Change a PR's body, title, or ticket links. |
| [`guildbotics member repo-info`](#guildbotics-member-repo-info) | Read information about the repository as a whole. |
| [`guildbotics member repo-info read`](#guildbotics-member-repo-info-read) | Read one bounded page of the repository's dependency_alerts, the repository named by its path. |
| [`guildbotics member task`](#guildbotics-member-task) | Completion records of runs other than a chat event's. |
| [`guildbotics member task complete`](#guildbotics-member-task-complete) | Finish a run other than a chat event's with evidence, revalidating affected PR readiness before accepting done. |
| [`guildbotics member task status`](#guildbotics-member-task-status) | Inspect the run's recorded evidence. |
| [`guildbotics member ticket`](#guildbotics-member-ticket) | Read, create, update, and comment on tickets, and react to their comments. |
| [`guildbotics member ticket comment`](#guildbotics-member-ticket-comment) | Comment on a ticket in the member voice. |
| [`guildbotics member ticket create`](#guildbotics-member-ticket-create) | Open a follow-up ticket a human asked for, in the tracker a ticket's read names (`tracker`); a board that keeps every ticket refuses `--no-add-to-board`. |
| [`guildbotics member ticket reaction`](#guildbotics-member-ticket-reaction) | Reactions to ticket comments. |
| [`guildbotics member ticket reaction add`](#guildbotics-member-ticket-reaction-add) | React to a comment of the ticket, by the id its read gave it. |
| [`guildbotics member ticket read`](#guildbotics-member-ticket-read) | Read one bounded page of a ticket's resource: tickets (the ticket itself: key, tracker, repository, lane, ...), ticket_comments, or ticket_pull_requests (the code host's pull requests that refer to... |
| [`guildbotics member ticket update`](#guildbotics-member-ticket-update) | Change a ticket's body, title, labels, state, parent, or the tickets it is blocked by; empty content removes the body, and an empty `--parent` the parent. |
| [`guildbotics run`](#guildbotics-run) | Run a command through the matching Desktop when open, otherwise locally. |
| [`guildbotics secrets`](#guildbotics-secrets) | Manage workspace secrets (API keys and tokens). |
| [`guildbotics secrets delete`](#guildbotics-secrets-delete) | Delete a stored secret. |
| [`guildbotics secrets export`](#guildbotics-secrets-export) | Export stored secrets in dotenv format (exchange format only). |
| [`guildbotics secrets import`](#guildbotics-secrets-import) | Import secrets from a dotenv-format exchange file into the OS store. |
| [`guildbotics secrets list`](#guildbotics-secrets-list) | List the names of the stored secrets. |
| [`guildbotics secrets pull`](#guildbotics-secrets-pull) | Fetch values from the hub into this machine's OS secret store. |
| [`guildbotics secrets push`](#guildbotics-secrets-push) | Send values entered on this machine to the hub (all it lacks by default). |
| [`guildbotics secrets set`](#guildbotics-secrets-set) | Store a secret value (prompts when VALUE is omitted). |
| [`guildbotics secrets status`](#guildbotics-secrets-status) | Show OS secret-store status for this workspace. |
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
| [`guildbotics diagnostics`](#guildbotics-diagnostics) | Read recorded diagnostics for a workspace (read-only). |
| [`guildbotics environment`](#guildbotics-environment) | Build and log in to the isolated agent environment on this device. |
| [`guildbotics git-credential`](#guildbotics-git-credential) | Hand git the human member's credential for a remote of a service GuildBotics is connected to on this machine (ACTION is git's: get, store, or erase). |
| [`guildbotics hub`](#guildbotics-hub) | Host a synchronization hub for GuildBotics workspaces on this machine. |
| [`guildbotics kill`](#guildbotics-kill) | Immediately force kill a CLI-managed background service. |
| [`guildbotics member`](#guildbotics-member) | Operate as a configured GuildBotics member. |
| [`guildbotics run`](#guildbotics-run) | Run a command through the matching Desktop when open, otherwise locally. |
| [`guildbotics secrets`](#guildbotics-secrets) | Manage workspace secrets (API keys and tokens). |
| [`guildbotics start`](#guildbotics-start) | Start GuildBotics runtimes (scheduler and event listener runner). |
| [`guildbotics stop`](#guildbotics-stop) | Gracefully stop a CLI-managed background service. |
| [`guildbotics version`](#guildbotics-version) | Print version. |
| [`guildbotics workspace`](#guildbotics-workspace) | Manage the active GuildBotics workspace used by AI CLI tools. |

## `guildbotics diagnostics`

Read recorded diagnostics for a workspace (read-only).

```text
guildbotics diagnostics [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--workspace DIRECTORY` | Workspace root to use instead of the persisted active workspace. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics diagnostics system`](#guildbotics-diagnostics-system) | Show service-wide records that belong to no single execution. |
| [`guildbotics diagnostics trace`](#guildbotics-diagnostics-trace) | Show one execution's summary and records. |
| [`guildbotics diagnostics traces`](#guildbotics-diagnostics-traces) | List recorded executions, newest first. |

## `guildbotics diagnostics system`

Show service-wide records that belong to no single execution.

```text
guildbotics diagnostics system [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--limit INTEGER RANGE` | Maximum entries. [default: 50; 1\<=x\<=1000] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics diagnostics trace`

Show one execution's summary and records.

```text
guildbotics diagnostics trace [OPTIONS] TRACE_ID
```

| Option | Description |
| --- | --- |
| `--kind [event\|log\|io\|memory]` | Only records of these kinds. Repeat to combine. |
| `--level TEXT` | Only log records at this level, such as 'error'. |
| `--limit INTEGER RANGE` | Maximum records, keeping the newest. [default: 200; 1\<=x\<=5000] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics diagnostics traces`

List recorded executions, newest first.

```text
guildbotics diagnostics traces [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--source TEXT` | Only traces from this execution source. |
| `--person TEXT` | Only traces recorded for this member. |
| `--query TEXT` | Free-text filter over each execution's summary and full transcript. |
| `--attr-key TEXT` | Only traces carrying this attribute key. |
| `--attr-value TEXT` | Required value for --attr-key. |
| `--limit INTEGER RANGE` | Maximum entries. [default: 50; 1\<=x\<=1000] |
| `--include-assistant / --no-include-assistant` | Include the Desktop AI assistants' own traces. [default: no-include-assistant] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics environment`

Build and log in to the isolated agent environment on this device.

```text
guildbotics environment [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--workspace DIRECTORY` | Workspace root to use instead of the persisted active workspace. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics environment build`](#guildbotics-environment-build) | Build the environment the shared declaration asks for. |
| [`guildbotics environment image`](#guildbotics-environment-image) | The base images this device holds for the environment. |
| [`guildbotics environment login`](#guildbotics-environment-login) | Log in to an AI CLI tool inside the environment. |
| [`guildbotics environment remove`](#guildbotics-environment-remove) | Remove the workspace's snapshots from this device (logins are kept). |
| [`guildbotics environment status`](#guildbotics-environment-status) | Show the environment's runtime, snapshot, and logins on this device. |

## `guildbotics environment build`

Build the environment the shared declaration asks for.

The build installs provider CLIs and nothing else, so it needs no input. A
build that fails is remembered until the declaration changes or this
command runs again.

```text
guildbotics environment build [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--force` | Rebuild even when the snapshot already matches the declaration. |
| `--help` | Show this message and exit. |

## `guildbotics environment image`

The base images this device holds for the environment.

A workspace may name an image it built itself as the base of the
environment; the image is not shared, so each device loads it from an
archive (`docker save`) built for its CPU architecture, and the
declaration names the image's digest per architecture.

```text
guildbotics environment image [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics environment image declare`](#guildbotics-environment-image-declare) | Name the base image in the workspace's shared declaration. |
| [`guildbotics environment image list`](#guildbotics-environment-image-list) | List the images this device's declaration may name as its base. |
| [`guildbotics environment image load`](#guildbotics-environment-image-load) | Load an image archive into this device's runtime. |
| [`guildbotics environment image load-ci`](#guildbotics-environment-image-load-ci) | Load an image archive into the CI runner of the workspace's code host. |

## `guildbotics environment image declare`

Name the base image in the workspace's shared declaration.

The declaration is shared with every device of the workspace. Naming the
same reference again merges the digests, so a device of another
architecture adds its own; naming another reference replaces them.

```text
guildbotics environment image declare [OPTIONS] [REFERENCE]
```

| Option | Description |
| --- | --- |
| `--digest ARCH=DIGEST` | Name the image's digest for an architecture (e.g. amd64=sha256:...). Repeatable. Without it, the digest of the image loaded here under REFERENCE is declared for this device's architecture. |
| `--default` | Declare no image: the environment builds from GuildBotics' own. |
| `--help` | Show this message and exit. |

## `guildbotics environment image list`

List the images this device's declaration may name as its base.

Those loaded here, by the name they were loaded under, and whether the
declaration names each for this device's architecture; the same list the
Desktop's declaration picks from.

```text
guildbotics environment image list [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics environment image load`

Load an image archive into this device's runtime.

The archive must be built for this device's CPU architecture. Its own
tags are kept. What was loaded is printed by reference, with the digest
the declaration names it by.

```text
guildbotics environment image load [OPTIONS] ARCHIVE
```

| Option | Description |
| --- | --- |
| `--tag TEXT` | A reference to add to the loaded image, beyond the archive's own tags. |
| `--help` | Show this message and exit. |

## `guildbotics environment image load-ci`

Load an image archive into the CI runner of the workspace's code host.

Run it on the machine the CI runner runs on, with an archive built for
that runner's CPU architecture: the code host reads the file there, by
its resolved path. Each image reference the runner loaded is printed. A
code host with no CI runner GuildBotics loads images into is left as it
is.

```text
guildbotics environment image load-ci [OPTIONS] ARCHIVE
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics environment login`

Log in to an AI CLI tool inside the environment.

The tool's own login command runs in the environment and talks you
through it here; what it stores stays on this device, outside the
snapshot, and every member uses it.

```text
guildbotics environment login [OPTIONS] {codex|claude|grok|copilot|antigravity}
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics environment remove`

Remove the workspace's snapshots from this device (logins are kept).

```text
guildbotics environment remove [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics environment status`

Show the environment's runtime, snapshot, and logins on this device.

```text
guildbotics environment status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics git-credential`

Hand git the human member's credential for a remote of a service
GuildBotics is connected to on this machine (ACTION is git's: get,
store, or erase).

Git runs this as a credential helper: add it, scoped to the service's
URL, at the end of the global git configuration. Setup shows the lines to
add.

```text
guildbotics git-credential [OPTIONS] ACTION
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics hub`

Host a synchronization hub for GuildBotics workspaces on this machine.

```text
guildbotics hub [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub create`](#guildbotics-hub-create) | Make this machine a hub, or show the hub it already hosts. |
| [`guildbotics hub identity`](#guildbotics-hub-identity) | Manage the repository of the members' identities, which every workspace shares. |
| [`guildbotics hub live`](#guildbotics-hub-live) | Publish and watch transient workspace live state. |
| [`guildbotics hub owner`](#guildbotics-hub-owner) | Inspect or explicitly change a workspace's service owner. |
| [`guildbotics hub secret`](#guildbotics-hub-secret) | Hold secret values for the devices that share a workspace. |
| [`guildbotics hub status`](#guildbotics-hub-status) | Show whether this machine hosts a hub, and which workspaces it holds. |
| [`guildbotics hub workspace`](#guildbotics-hub-workspace) | Manage the repositories this hub holds, one per workspace. |

## `guildbotics hub create`

Make this machine a hub, or show the hub it already hosts.

```text
guildbotics hub create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub identity`

Manage the repository of the members' identities, which every workspace
shares.

```text
guildbotics hub identity [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub identity claim`](#guildbotics-hub-identity-claim) | Show the identities' repository, registering IDENTITY_ID if there is none. |

## `guildbotics hub identity claim`

Show the identities' repository, registering IDENTITY_ID if there is none.

```text
guildbotics hub identity claim [OPTIONS] IDENTITY_ID
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub live`

Publish and watch transient workspace live state.

```text
guildbotics hub live [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub live publish`](#guildbotics-hub-live-publish) | Replace one live snapshot for each non-empty stdin line. |
| [`guildbotics hub live watch`](#guildbotics-hub-live-watch) | Poll live snapshots and head updates until the SSH connection closes. |

## `guildbotics hub live publish`

Replace one live snapshot for each non-empty stdin line.

```text
guildbotics hub live publish [OPTIONS] WORKSPACE_ID DEVICE_ID PUBLISHER_ID
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics hub live watch`

Poll live snapshots and head updates until the SSH connection closes.

```text
guildbotics hub live watch [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics hub owner`

Inspect or explicitly change a workspace's service owner.

```text
guildbotics hub owner [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub owner claim`](#guildbotics-hub-owner-claim) | Claim ownership only when the workspace has no owner. |
| [`guildbotics hub owner get`](#guildbotics-hub-owner-get) | Show the current service owner, if one has been claimed. |
| [`guildbotics hub owner transfer`](#guildbotics-hub-owner-transfer) | Move ownership explicitly to another device. |

## `guildbotics hub owner claim`

Claim ownership only when the workspace has no owner.

```text
guildbotics hub owner claim [OPTIONS] WORKSPACE_ID DEVICE_ID
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub owner get`

Show the current service owner, if one has been claimed.

```text
guildbotics hub owner get [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub owner transfer`

Move ownership explicitly to another device.

```text
guildbotics hub owner transfer [OPTIONS] WORKSPACE_ID DEVICE_ID
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub secret`

Hold secret values for the devices that share a workspace.

Values arrive and leave on this command's own standard input and output.
They are never written to the workspace repository, to a relay file, or to
a temporary file, and never appear in output, logs, or error messages.

```text
guildbotics hub secret [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub secret list`](#guildbotics-hub-secret-list) | Report which generation this hub holds for each key, without values. |
| [`guildbotics hub secret receive`](#guildbotics-hub-secret-receive) | Store the values a device sends, one framed entry each. |
| [`guildbotics hub secret send`](#guildbotics-hub-secret-send) | Write the requested values back to the device, one framed entry each. |

## `guildbotics hub secret list`

Report which generation this hub holds for each key, without values.

```text
guildbotics hub secret list [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics hub secret receive`

Store the values a device sends, one framed entry each.

```text
guildbotics hub secret receive [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics hub secret send`

Write the requested values back to the device, one framed entry each.

```text
guildbotics hub secret send [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--key TEXT` | A logical key to send back. |
| `--help` | Show this message and exit. |

## `guildbotics hub status`

Show whether this machine hosts a hub, and which workspaces it holds.

```text
guildbotics hub status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub workspace`

Manage the repositories this hub holds, one per workspace.

```text
guildbotics hub workspace [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics hub workspace create`](#guildbotics-hub-workspace-create) | Create the repository a workspace synchronizes through. |
| [`guildbotics hub workspace list`](#guildbotics-hub-workspace-list) | List the workspaces this hub holds. |

## `guildbotics hub workspace create`

Create the repository a workspace synchronizes through.

```text
guildbotics hub workspace create [OPTIONS] WORKSPACE_ID
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics hub workspace list`

List the workspaces this hub holds.

```text
guildbotics hub workspace list [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

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
| [`guildbotics member chat`](#guildbotics-member-chat) | Chat identity, posting, replies, reactions, and run completion. |
| [`guildbotics member checks`](#guildbotics-member-checks) | End a run to await the CI checks of its pull requests. |
| [`guildbotics member context`](#guildbotics-member-context) | Show non-secret member context. |
| [`guildbotics member git`](#guildbotics-member-git) | Prepare, commit, push, and publish member git workspaces. |
| [`guildbotics member help`](#guildbotics-member-help) | Print the member capability reference (commands and cross-cutting rules). |
| [`guildbotics member memory`](#guildbotics-member-memory) | Record, recall, and maintain member memory documents. |
| [`guildbotics member pr`](#guildbotics-member-pr) | Read, open, update, comment on, and review pull requests, react to their comments and reviews, and download their CI artifacts. |
| [`guildbotics member repo-info`](#guildbotics-member-repo-info) | Read information about the repository as a whole. |
| [`guildbotics member task`](#guildbotics-member-task) | Completion records of runs other than a chat event's. |
| [`guildbotics member ticket`](#guildbotics-member-ticket) | Read, create, update, and comment on tickets, and react to their comments. |

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
| [`guildbotics member chat complete`](#guildbotics-member-chat-complete) | Finish the chat workflow run on its event with evidence. |
| [`guildbotics member chat identity`](#guildbotics-member-chat-identity) | Show the member's chat identity. |
| [`guildbotics member chat inspect`](#guildbotics-member-chat-inspect) | Inspect chat channel or thread messages for interactive decisions. |
| [`guildbotics member chat noop`](#guildbotics-member-chat-noop) | Record a deliberate no-op for the chat run's event. |
| [`guildbotics member chat post`](#guildbotics-member-chat-post) | Post a new channel message. |
| [`guildbotics member chat reaction`](#guildbotics-member-chat-reaction) | Chat reaction operations. |
| [`guildbotics member chat reply`](#guildbotics-member-chat-reply) | Reply in a thread in the member voice. |
| [`guildbotics member chat resolve-channel`](#guildbotics-member-chat-resolve-channel) | Resolve a channel name to its channel id without reading or posting; the id is empty when no channel has that name. |
| [`guildbotics member chat updates`](#guildbotics-member-chat-updates) | Check the source thread's durable queue before replying or publishing in a chat workflow; reconsider new messages, and stop external writes when reception is unavailable. |

## `guildbotics member chat complete`

Finish the chat workflow run on its event with evidence.

```text
guildbotics member chat complete [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--status [done\|asking\|blocked]` | Run outcome. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
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
| `--format [json\|markdown]` | Output format. [default: markdown] |
| `--help` | Show this message and exit. |

## `guildbotics member chat inspect`

Inspect chat channel or thread messages for interactive decisions.

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
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--since TEXT` | Only include messages that occurred at or after this time (ISO 8601; local time without an offset). |
| `--until TEXT` | Only include messages that occurred at or before this time (ISO 8601; local time without an offset). |
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
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--thread-id TEXT` | Thread id (with --channel-id). [default: ""] |
| `--message-url TEXT` | Chat message URL (alternative to --channel-id with --thread-id). [default: ""] |
| `--limit INTEGER RANGE` | Maximum number of messages. [default: 100; 1\<=x\<=200] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat noop`

Record a deliberate no-op for the chat run's event.

```text
guildbotics member chat noop [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
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
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
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
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--message-id TEXT` | Message id (with --channel-id). [default: ""] |
| `--message-url TEXT` | Chat message URL (alternative to --channel-id with --message-id). [default: ""] |
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
| `--channel-id TEXT` | Channel id of the target channel. [default: ""] |
| `--channel-name TEXT` | Channel name (alternative to --channel-id). [default: ""] |
| `--thread-id TEXT` | Thread id (with --channel-id). [default: ""] |
| `--message-url TEXT` | Chat message URL (alternative to --channel-id with --thread-id). [default: ""] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat resolve-channel`

Resolve a channel name to its channel id without reading or posting; the id is empty when no channel has that name.

```text
guildbotics member chat resolve-channel [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--channel-name TEXT` | Channel name to resolve. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member chat updates`

Check the source thread's durable queue before replying or publishing in a chat workflow; reconsider new messages, and stop external writes when reception is unavailable.

```text
guildbotics member chat updates [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member checks`

End a run to await the CI checks of its pull requests.

```text
guildbotics member checks [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member checks await`](#guildbotics-member-checks-await) | End any run -- a ticket's, a chat thread's, or a command's -- to await its own PRs' pending checks: a notice on each PR, from which the patrol resumes the run's work once they settle. |

## `guildbotics member checks await`

End any run -- a ticket's, a chat thread's, or a command's -- to await its own PRs' pending checks: a notice on each PR, from which the patrol resumes the run's work once they settle.

```text
guildbotics member checks await [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
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
| [`guildbotics member git prepare`](#guildbotics-member-git-prepare) | Clone/checkout an isolated member workspace: a ticket branch `ticket/\<key\>` (--issue-url, with --repo for a ticket that belongs to no repository), a PR head (--pr-url, alone or together with... |
| [`guildbotics member git publish`](#guildbotics-member-git-publish) | Commit already-staged changes with the member identity, then push. |
| [`guildbotics member git push`](#guildbotics-member-git-push) | Push the current branch with the member credential and report readiness for matching open PRs. |

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
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. [default: member] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member git prepare`

Clone/checkout an isolated member workspace: a ticket branch `ticket/<key>` (--issue-url, with --repo for a ticket that belongs to no repository), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo --branch). A repository is named by its path. Unfinished checkouts stay assigned to their work and requested branch; idle checkouts of the same repository are reused with their build caches. Call prepare every run and use the returned repo_path and branch: a path from an earlier run may now belong to another work. Unfinished Git state is kept, including an agent-created branch; an empty branch means detached HEAD. Member workspaces are worked on only inside a running GuildBotics command, whose isolated environment runs every git of them.

```text
guildbotics member git prepare [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--issue-url TEXT` | Ticket URL to prepare a workspace for. [default: ""] |
| `--pr-url TEXT` | PR URL whose head branch to check out. [default: ""] |
| `--repo TEXT` | Repository path (owner/repo on GitHub): with --branch, or with --issue-url for a ticket that belongs to no repository. [default: ""] |
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
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--workspace-mode [member\|current]` | Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session. [default: member] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member git push`

Push the current branch with the member credential and report readiness for matching open PRs.

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
| [`guildbotics member memory get`](#guildbotics-member-memory-get) | Read one memory document's metadata and body without changing recency. |
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

Read one memory document's metadata and body without changing recency.

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
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
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
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--policy-approved` | Confirm that a human approved this policy memory change. |
| `--set TEXT` | Extra metadata as key=value. May be repeated. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr`

Read, open, update, comment on, and review pull requests, react to their
comments and reviews, and download their CI artifacts.

```text
guildbotics member pr [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member pr artifact`](#guildbotics-member-pr-artifact) | CI artifact operations. |
| [`guildbotics member pr comment`](#guildbotics-member-pr-comment) | Comment on a PR conversation. |
| [`guildbotics member pr create`](#guildbotics-member-pr-create) | Open a PR, or return the existing open PR for the same head and base branches. |
| [`guildbotics member pr reaction`](#guildbotics-member-pr-reaction) | Reactions to pull request comments and reviews. |
| [`guildbotics member pr read`](#guildbotics-member-pr-read) | Read one bounded page of a PR's resource: pull_requests, pull_request_comments (conversation comments), pull_request_reviews, pull_request_files, pull_request_threads, review_thread_comments, or... |
| [`guildbotics member pr reply`](#guildbotics-member-pr-reply) | Reply to an inline review thread. |
| [`guildbotics member pr review`](#guildbotics-member-pr-review) | Submit a review verdict as a review on the head the member read (`head_sha` of the PR's read); a conversation comment does not consume a review request or make the member a reviewer. |
| [`guildbotics member pr review-comment`](#guildbotics-member-pr-review-comment) | Create a new inline review comment on a diff line of the head the member read; when the head has moved, read the new commits and comment again. |
| [`guildbotics member pr update`](#guildbotics-member-pr-update) | Change a PR's body, title, or ticket links. |

## `guildbotics member pr artifact`

CI artifact operations.

```text
guildbotics member pr artifact [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member pr artifact download`](#guildbotics-member-pr-artifact-download) | Download and extract a size-limited CI artifact of the PR's head; remove repository files after inspection. |

## `guildbotics member pr artifact download`

Download and extract a size-limited CI artifact of the PR's head; remove repository files after inspection.

```text
guildbotics member pr artifact download [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--name TEXT` | Exact artifact name. [required] |
| `--dest DIRECTORY` | Directory to extract into. Defaults to the current directory. Remove downloaded files after inspection when this is inside a repository. [default: .] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr comment`

Comment on a PR conversation.

```text
guildbotics member pr comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr create`

Open a PR, or return the existing open PR for the same head and base branches. --issue-url, which may be repeated, links each ticket in the PR body as a reference, or with --closes-issue so that merging the PR closes every one of them; a code host that cannot close one of those tickets on a merge refuses --closes-issue.

```text
guildbotics member pr create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--repo TEXT` | Target repository path (owner/repo on GitHub). [required] |
| `--head TEXT` | Head branch containing the changes. [required] |
| `--base TEXT` | Base branch for the pull request. Defaults to the repository default branch. [default: ""] |
| `--title TEXT` | Pull request title. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--issue-url TEXT` | Related ticket URL to link to the PR. May be repeated. |
| `--closes-issue / --refs-issue` | Link every --issue-url ticket so that merging the PR closes it, or only refer to it. Defaults to referring; a code host that cannot close one of the tickets on a merge refuses --closes-issue. [default: refs-issue] |
| `--draft [true\|false]` | Open the pull request as a draft. [default: false] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr reaction`

Reactions to pull request comments and reviews.

```text
guildbotics member pr reaction [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member pr reaction add`](#guildbotics-member-pr-reaction-add) | React to a conversation comment, a review thread comment, or a review's body of the PR, by the id its read gave it. |

## `guildbotics member pr reaction add`

React to a conversation comment, a review thread comment, or a review's body of the PR, by the id its read gave it.

```text
guildbotics member pr reaction add [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--comment-id TEXT` | The id the item's read gave the comment (or review) to react to. [required] |
| `--reaction [ack\|agree\|celebrate\|support]` | Semantic reaction to add. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr read`

Read one bounded page of a PR's resource: pull_requests, pull_request_comments (conversation comments), pull_request_reviews, pull_request_files, pull_request_threads, review_thread_comments, or pull_request_readiness. Collections accept page_size (1-100); review_thread_comments also requires node from pull_request_threads. Readiness accepts failed_logs (boolean) and log_tail_bytes (1-65536, default 65536). Results contain normalized items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

```text
guildbotics member pr read [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--resource [pull_requests\|pull_request_comments\|pull_request_reviews\|pull_request_files\|pull_request_threads\|review_thread_comments\|pull_request_readiness]` | Resource to read; see member help for its conditions. [required] |
| `--subject TEXT` | What the resource is part of: the ticket's or pull request's URL, or the repository's path. [required] |
| `--params TEXT` | Resource conditions as a JSON object. [default: {}] |
| `--continuation TEXT` | Continuation returned for the same resource and conditions. [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr reply`

Reply to an inline review thread.

```text
guildbotics member pr reply [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--reply-target-id TEXT` | reply_target_id from repository/pr_inspect include_comments=true. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr review`

Submit a review verdict as a review on the head the member read (`head_sha` of the PR's read); a conversation comment does not consume a review request or make the member a reviewer. When the head has moved, nothing is written: read the new commits, then submit again on the new head. A code host without request_changes refuses it.

```text
guildbotics member pr review [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--event [approve\|request_changes\|comment]` | Review verdict submitted on the head --head names. [required] |
| `--head TEXT` | The head commit (head_sha) the member read; refused when the PR's head has moved since. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr review-comment`

Create a new inline review comment on a diff line of the head the member read; when the head has moved, read the new commits and comment again.

```text
guildbotics member pr review-comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--head TEXT` | The head commit (head_sha) the member read; refused when the PR's head has moved since. [required] |
| `--path TEXT` | File path in the PR diff. [required] |
| `--line INTEGER RANGE` | Line number on the chosen diff side. [x\>=1; required] |
| `--side [LEFT\|RIGHT]` | Diff side of the line. [default: RIGHT] |
| `--start-line INTEGER RANGE` | Start line for a multi-line comment. [x\>=1] |
| `--start-side [LEFT\|RIGHT]` | Diff side of --start-line. |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member pr update`

Change a PR's body, title, or ticket links. --issue-url replaces all links with those tickets; --closes-issue closes all of them on merge and --refs-issue only refers (the default). --drop-issue-links removes all links without requiring a body. Without either option, body replacement preserves existing ticket links and ignores new managed link lines in the body.

```text
guildbotics member pr update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Pull request URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--title TEXT` | Replace the pull request title. |
| `--issue-url TEXT` | Replace all PR ticket links with these URLs. May be repeated. |
| `--closes-issue / --refs-issue` | Close every --issue-url ticket on merge, or only refer to it (default). [default: refs-issue] |
| `--drop-issue-links` | Remove all PR ticket links. Cannot be combined with --issue-url. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member repo-info`

Read information about the repository as a whole.

```text
guildbotics member repo-info [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member repo-info read`](#guildbotics-member-repo-info-read) | Read one bounded page of the repository's dependency_alerts, the repository named by its path. |

## `guildbotics member repo-info read`

Read one bounded page of the repository's dependency_alerts, the repository named by its path. It accepts state (open by default, resolved, dismissed) and page_size (1-100, default 30). With an id condition, it returns that one alert and accepts no other condition or continuation. For formatted or JSON alerts, run repository/security_alerts in a ready command environment. Results contain normalized items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

```text
guildbotics member repo-info read [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--resource [dependency_alerts]` | Resource to read; see member help for its conditions. [required] |
| `--subject TEXT` | What the resource is part of: the ticket's or pull request's URL, or the repository's path. [required] |
| `--params TEXT` | Resource conditions as a JSON object. [default: {}] |
| `--continuation TEXT` | Continuation returned for the same resource and conditions. [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member task`

Completion records of runs other than a chat event's.

```text
guildbotics member task [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member task complete`](#guildbotics-member-task-complete) | Finish a run other than a chat event's with evidence, revalidating affected PR readiness before accepting done. |
| [`guildbotics member task status`](#guildbotics-member-task-status) | Inspect the run's recorded evidence. |

## `guildbotics member task complete`

Finish a run other than a chat event's with evidence, revalidating affected PR readiness before accepting done.

```text
guildbotics member task complete [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--status [done\|asking\|blocked]` | Run outcome. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member task status`

Inspect the run's recorded evidence.

```text
guildbotics member task status [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Accepted for consistency with other member commands; not required. [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member ticket`

Read, create, update, and comment on tickets, and react to their comments.

```text
guildbotics member ticket [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member ticket comment`](#guildbotics-member-ticket-comment) | Comment on a ticket in the member voice. |
| [`guildbotics member ticket create`](#guildbotics-member-ticket-create) | Open a follow-up ticket a human asked for, in the tracker a ticket's read names (`tracker`); a board that keeps every ticket refuses `--no-add-to-board`. |
| [`guildbotics member ticket reaction`](#guildbotics-member-ticket-reaction) | Reactions to ticket comments. |
| [`guildbotics member ticket read`](#guildbotics-member-ticket-read) | Read one bounded page of a ticket's resource: tickets (the ticket itself: key, tracker, repository, lane, ...), ticket_comments, or ticket_pull_requests (the code host's pull requests that refer to... |
| [`guildbotics member ticket update`](#guildbotics-member-ticket-update) | Change a ticket's body, title, labels, state, parent, or the tickets it is blocked by; empty content removes the body, and an empty `--parent` the parent. |

## `guildbotics member ticket comment`

Comment on a ticket in the member voice.

```text
guildbotics member ticket comment [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Ticket URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member ticket create`

Open a follow-up ticket a human asked for, in the tracker a ticket's read names (`tracker`); a board that keeps every ticket refuses `--no-add-to-board`.

```text
guildbotics member ticket create [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--tracker TEXT` | Where the ticket is kept: a repository path (owner/repo) on GitHub. [required] |
| `--title TEXT` | Ticket title. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--label TEXT` | Label already defined in the tracker. Repeat for several labels. |
| `--add-to-board / --no-add-to-board` | Put the created ticket on the configured board; a board that keeps every ticket refuses --no-add-to-board. [default: add-to-board] |
| `--human-approved` | Confirm that a human instructed or approved this change. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member ticket reaction`

Reactions to ticket comments.

```text
guildbotics member ticket reaction [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics member ticket reaction add`](#guildbotics-member-ticket-reaction-add) | React to a comment of the ticket, by the id its read gave it. |

## `guildbotics member ticket reaction add`

React to a comment of the ticket, by the id its read gave it.

```text
guildbotics member ticket reaction add [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Ticket URL. [required] |
| `--comment-id TEXT` | The id the item's read gave the comment (or review) to react to. [required] |
| `--reaction [ack\|agree\|celebrate\|support]` | Semantic reaction to add. [required] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member ticket read`

Read one bounded page of a ticket's resource: tickets (the ticket itself: key, tracker, repository, lane, ...), ticket_comments, or ticket_pull_requests (the code host's pull requests that refer to it). Collections accept page_size (1-100). Results contain normalized items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

```text
guildbotics member ticket read [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--resource [tickets\|ticket_comments\|ticket_pull_requests]` | Resource to read; see member help for its conditions. [required] |
| `--subject TEXT` | What the resource is part of: the ticket's or pull request's URL, or the repository's path. [required] |
| `--params TEXT` | Resource conditions as a JSON object. [default: {}] |
| `--continuation TEXT` | Continuation returned for the same resource and conditions. [default: ""] |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics member ticket update`

Change a ticket's body, title, labels, state, parent, or the tickets it is blocked by; empty content removes the body, and an empty `--parent` the parent.

```text
guildbotics member ticket update [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name of the member. [required] |
| `--url TEXT` | Ticket URL. [required] |
| `--content-stdin` | Read the command's entire free-form content from standard input. |
| `--content-file FILE` | Read the command's entire free-form content from a UTF-8 file. |
| `--title TEXT` | Replace the ticket title. |
| `--add-label TEXT` | Add a label already defined in the tracker. Repeat for several labels. |
| `--remove-label TEXT` | Remove a label from the ticket. Repeat for several labels. |
| `--state [open\|closed]` | Close or reopen the ticket; requires --human-approved. |
| `--state-reason [completed\|not_planned]` | Why the ticket is closed; requires --state closed. |
| `--parent TEXT` | URL of the ticket this one becomes a part of; "" makes it a part of none. |
| `--add-blocked-by TEXT` | URL of a ticket that must be closed before this one is started. Repeat for several tickets. |
| `--remove-blocked-by TEXT` | URL of a ticket this one is no longer blocked by. Repeat for several tickets. |
| `--human-approved` | Confirm that a human instructed or approved this change. |
| `--format [json\|markdown]` | Output format. [default: json] |
| `--help` | Show this message and exit. |

## `guildbotics run`

Run a command through the matching Desktop when open, otherwise locally.

```text
guildbotics run [OPTIONS] CUSTOM_COMMAND [COMMAND_ARGS]...
```

| Option | Description |
| --- | --- |
| `--person TEXT` | Person ID or name to run the custom command as. Defaults to the team's default_person_id, else its first active non-human member in ID order. |
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
| `--identities` | Manage the secrets every workspace shares -- the members' credentials and the services' connections -- instead of the workspace's. |
| `--help` | Show this message and exit. |

| Subcommand | Summary |
| --- | --- |
| [`guildbotics secrets delete`](#guildbotics-secrets-delete) | Delete a stored secret. |
| [`guildbotics secrets export`](#guildbotics-secrets-export) | Export stored secrets in dotenv format (exchange format only). |
| [`guildbotics secrets import`](#guildbotics-secrets-import) | Import secrets from a dotenv-format exchange file into the OS store. |
| [`guildbotics secrets list`](#guildbotics-secrets-list) | List the names of the stored secrets. |
| [`guildbotics secrets pull`](#guildbotics-secrets-pull) | Fetch values from the hub into this machine's OS secret store. |
| [`guildbotics secrets push`](#guildbotics-secrets-push) | Send values entered on this machine to the hub (all it lacks by default). |
| [`guildbotics secrets set`](#guildbotics-secrets-set) | Store a secret value (prompts when VALUE is omitted). |
| [`guildbotics secrets status`](#guildbotics-secrets-status) | Show OS secret-store status for this workspace. |

## `guildbotics secrets delete`

Delete a stored secret.

```text
guildbotics secrets delete [OPTIONS] KEY
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets export`

Export stored secrets in dotenv format (exchange format only).

```text
guildbotics secrets export [OPTIONS]
```

| Option | Description |
| --- | --- |
| `--file FILE` | Write to a file (created with owner-only permissions) instead of stdout. |
| `--help` | Show this message and exit. |

## `guildbotics secrets import`

Import secrets from a dotenv-format exchange file into the OS store.

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

## `guildbotics secrets pull`

Fetch values from the hub into this machine's OS secret store.

With no KEYS this is the whole of setting up a new device: every key this
machine has no value for, or holds an older value for, arrives in one
exchange and is stored without anything being retyped.

The shared files are taken first: only the generation they name is ever
adopted, so an old copy of them would leave nothing to fetch.

```text
guildbotics secrets pull [OPTIONS] [KEYS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets push`

Send values entered on this machine to the hub (all it lacks by default).

The shared files are refreshed first. Which generation a value is sent from
is read out of ``config/secrets.yml``, and a machine that has been offline
would otherwise send from a generation the others have moved past.

```text
guildbotics secrets push [OPTIONS] [KEYS]...
```

| Option | Description |
| --- | --- |
| `--help` | Show this message and exit. |

## `guildbotics secrets set`

Store a secret value (prompts when VALUE is omitted).

KEY comes first: naming the value first would store it under a name that
is itself the secret.

```text
guildbotics secrets set [OPTIONS] KEY [VALUE]
```

| Option | Description |
| --- | --- |
| `--from-file FILE` | Read the secret value from a file instead of VALUE. |
| `--help` | Show this message and exit. |

## `guildbotics secrets status`

Show OS secret-store status for this workspace.

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
| `--force` | Cancel in-flight work after timeout, then force terminate as a last resort |
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
