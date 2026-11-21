# GuildBotics CLI Reference

<!-- AUTO-GENERATED FILE. DO NOT EDIT BY HAND. -->

Regenerate this reference from the Rust CLI.

```sh
cargo run --locked -p guildbotics -- generate-cli-reference
```

For workspace setup and examples, see the [README](../README.md).

## Commands

- [`guildbotics`](#guildbotics)
- [`guildbotics environment`](#guildbotics-environment)
- [`guildbotics environment status`](#guildbotics-environment-status)
- [`guildbotics environment build`](#guildbotics-environment-build)
- [`guildbotics environment prune`](#guildbotics-environment-prune)
- [`guildbotics environment remove`](#guildbotics-environment-remove)
- [`guildbotics environment login`](#guildbotics-environment-login)
- [`guildbotics environment usage`](#guildbotics-environment-usage)
- [`guildbotics environment image`](#guildbotics-environment-image)
- [`guildbotics environment image load`](#guildbotics-environment-image-load)
- [`guildbotics environment image list`](#guildbotics-environment-image-list)
- [`guildbotics environment image declare`](#guildbotics-environment-image-declare)
- [`guildbotics server`](#guildbotics-server)
- [`guildbotics server hub-data`](#guildbotics-server-hub-data)
- [`guildbotics server hub-data backup`](#guildbotics-server-hub-data-backup)
- [`guildbotics server hub-data restore`](#guildbotics-server-hub-data-restore)
- [`guildbotics server hub-data check`](#guildbotics-server-hub-data-check)
- [`guildbotics server hub-data upgrade`](#guildbotics-server-hub-data-upgrade)
- [`guildbotics server hub-data reindex`](#guildbotics-server-hub-data-reindex)
- [`guildbotics server start`](#guildbotics-server-start)
- [`guildbotics server stop`](#guildbotics-server-stop)
- [`guildbotics member`](#guildbotics-member)
- [`guildbotics member chat`](#guildbotics-member-chat)
- [`guildbotics member chat complete`](#guildbotics-member-chat-complete)
- [`guildbotics member chat identity`](#guildbotics-member-chat-identity)
- [`guildbotics member chat inspect`](#guildbotics-member-chat-inspect)
- [`guildbotics member chat inspect channel`](#guildbotics-member-chat-inspect-channel)
- [`guildbotics member chat inspect thread`](#guildbotics-member-chat-inspect-thread)
- [`guildbotics member chat noop`](#guildbotics-member-chat-noop)
- [`guildbotics member chat post`](#guildbotics-member-chat-post)
- [`guildbotics member chat reaction`](#guildbotics-member-chat-reaction)
- [`guildbotics member chat reaction add`](#guildbotics-member-chat-reaction-add)
- [`guildbotics member chat reply`](#guildbotics-member-chat-reply)
- [`guildbotics member chat resolve-channel`](#guildbotics-member-chat-resolve-channel)
- [`guildbotics member chat updates`](#guildbotics-member-chat-updates)
- [`guildbotics member checks`](#guildbotics-member-checks)
- [`guildbotics member checks await`](#guildbotics-member-checks-await)
- [`guildbotics member checks retry`](#guildbotics-member-checks-retry)
- [`guildbotics member context`](#guildbotics-member-context)
- [`guildbotics member git`](#guildbotics-member-git)
- [`guildbotics member git commit`](#guildbotics-member-git-commit)
- [`guildbotics member git prepare`](#guildbotics-member-git-prepare)
- [`guildbotics member git publish`](#guildbotics-member-git-publish)
- [`guildbotics member git push`](#guildbotics-member-git-push)
- [`guildbotics member git show`](#guildbotics-member-git-show)
- [`guildbotics member help`](#guildbotics-member-help)
- [`guildbotics member memory`](#guildbotics-member-memory)
- [`guildbotics member memory archive`](#guildbotics-member-memory-archive)
- [`guildbotics member memory get`](#guildbotics-member-memory-get)
- [`guildbotics member memory promote`](#guildbotics-member-memory-promote)
- [`guildbotics member memory recall`](#guildbotics-member-memory-recall)
- [`guildbotics member memory record`](#guildbotics-member-memory-record)
- [`guildbotics member memory touch`](#guildbotics-member-memory-touch)
- [`guildbotics member memory update`](#guildbotics-member-memory-update)
- [`guildbotics member pr`](#guildbotics-member-pr)
- [`guildbotics member pr artifact`](#guildbotics-member-pr-artifact)
- [`guildbotics member pr artifact download`](#guildbotics-member-pr-artifact-download)
- [`guildbotics member pr comment`](#guildbotics-member-pr-comment)
- [`guildbotics member pr create`](#guildbotics-member-pr-create)
- [`guildbotics member pr reaction`](#guildbotics-member-pr-reaction)
- [`guildbotics member pr reaction add`](#guildbotics-member-pr-reaction-add)
- [`guildbotics member pr read`](#guildbotics-member-pr-read)
- [`guildbotics member pr reply`](#guildbotics-member-pr-reply)
- [`guildbotics member pr review`](#guildbotics-member-pr-review)
- [`guildbotics member pr review-comment`](#guildbotics-member-pr-review-comment)
- [`guildbotics member pr update`](#guildbotics-member-pr-update)
- [`guildbotics member repo-info`](#guildbotics-member-repo-info)
- [`guildbotics member repo-info read`](#guildbotics-member-repo-info-read)
- [`guildbotics member task`](#guildbotics-member-task)
- [`guildbotics member task complete`](#guildbotics-member-task-complete)
- [`guildbotics member task status`](#guildbotics-member-task-status)
- [`guildbotics member ticket`](#guildbotics-member-ticket)
- [`guildbotics member ticket attachment`](#guildbotics-member-ticket-attachment)
- [`guildbotics member ticket attachment download`](#guildbotics-member-ticket-attachment-download)
- [`guildbotics member ticket comment`](#guildbotics-member-ticket-comment)
- [`guildbotics member ticket create`](#guildbotics-member-ticket-create)
- [`guildbotics member ticket reaction`](#guildbotics-member-ticket-reaction)
- [`guildbotics member ticket reaction add`](#guildbotics-member-ticket-reaction-add)
- [`guildbotics member ticket read`](#guildbotics-member-ticket-read)
- [`guildbotics member ticket update`](#guildbotics-member-ticket-update)
- [`guildbotics git-credential`](#guildbotics-git-credential)
- [`guildbotics install`](#guildbotics-install)
- [`guildbotics workspace`](#guildbotics-workspace)
- [`guildbotics workspace use`](#guildbotics-workspace-use)
- [`guildbotics workspace current`](#guildbotics-workspace-current)
- [`guildbotics workspace status`](#guildbotics-workspace-status)
- [`guildbotics run`](#guildbotics-run)
- [`guildbotics diagnostics`](#guildbotics-diagnostics)
- [`guildbotics diagnostics traces`](#guildbotics-diagnostics-traces)
- [`guildbotics diagnostics trace`](#guildbotics-diagnostics-trace)
- [`guildbotics diagnostics system`](#guildbotics-diagnostics-system)
- [`guildbotics version`](#guildbotics-version)
- [`guildbotics secrets`](#guildbotics-secrets)
- [`guildbotics secrets status`](#guildbotics-secrets-status)
- [`guildbotics secrets list`](#guildbotics-secrets-list)
- [`guildbotics secrets set`](#guildbotics-secrets-set)
- [`guildbotics secrets delete`](#guildbotics-secrets-delete)
- [`guildbotics secrets export`](#guildbotics-secrets-export)
- [`guildbotics secrets import`](#guildbotics-secrets-import)
- [`guildbotics service`](#guildbotics-service)
- [`guildbotics service start`](#guildbotics-service-start)
- [`guildbotics service stop`](#guildbotics-service-stop)
- [`guildbotics connect`](#guildbotics-connect)
- [`guildbotics open`](#guildbotics-open)
- [`guildbotics runner`](#guildbotics-runner)
- [`guildbotics runner join`](#guildbotics-runner-join)
- [`guildbotics runner register`](#guildbotics-runner-register)
- [`guildbotics runner pull`](#guildbotics-runner-pull)
- [`guildbotics runner doctor`](#guildbotics-runner-doctor)
- [`guildbotics runner install-msb`](#guildbotics-runner-install-msb)

## guildbotics

```text
GuildBotics

Usage: guildbotics <COMMAND>

Commands:
  environment     Manage this device's isolated environment
  server          Run the server
  member          Operate as a configured member through the running server
  git-credential  Supply Git with this machine's credential; issuing a token requires the server
  install         Install this executable and the GuildBotics skill for this user
  workspace       Select the active workspace for the server and AI CLI tools
  run             Run a command on the server in the selected workspace's isolated environment
  diagnostics     Read a workspace's recorded diagnostics
  version         Print the binary's version
  secrets         Manage workspace or shared identity secrets
  service         Start or stop scheduled commands, ticket patrol, and chat events
  connect         Connect this machine's CLI through the server's consent screen
  open            Open the GuildBotics screens in the browser. The browser enters with a shared single-use link that expires after 60 seconds. The session also opens Branvil
  runner          Run the CI runner, a separate process that executes jobs in microVMs
  help            Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help

  -V, --version
          Print version
```

## guildbotics environment

```text
Manage this device's isolated environment

Usage: guildbotics environment [OPTIONS] <COMMAND>

Commands:
  status  Read the device's environment and refusal or warning
  build   Build the declared environment on this device
  prune   Remove obsolete snapshots and unreferenced image versions, retaining tags
  remove  Remove device snapshots while preserving provider logins
  login   Log in to a provider in its private environment
  usage   Probe provider usage in its private environment
  image   Manage local and CI base images
  help    Print this message or the help of the given subcommand(s)

Options:
      --workspace <WORKSPACE>
          Select the workspace whose environment declaration to use

  -h, --help
          Print help
```

## guildbotics environment status

```text
Read the device's environment and refusal or warning

Usage: guildbotics environment status [OPTIONS]

Options:
      --format <FORMAT>
          Select the output format

          [default: markdown]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics environment build

```text
Build the declared environment on this device

Usage: guildbotics environment build [OPTIONS]

Options:
      --force
          Rebuild even when the snapshot already matches the declaration

  -h, --help
          Print help
```

## guildbotics environment prune

```text
Remove obsolete snapshots and unreferenced image versions, retaining tags

Usage: guildbotics environment prune

Options:
  -h, --help
          Print help
```

## guildbotics environment remove

```text
Remove device snapshots while preserving provider logins

Usage: guildbotics environment remove

Options:
  -h, --help
          Print help
```

## guildbotics environment login

```text
Log in to a provider in its private environment

Usage: guildbotics environment login <TOOL>

Arguments:
  <TOOL>


Options:
  -h, --help
          Print help
```

## guildbotics environment usage

```text
Probe provider usage in its private environment

Usage: guildbotics environment usage <TOOL>

Arguments:
  <TOOL>


Options:
  -h, --help
          Print help
```

## guildbotics environment image

```text
Manage local and CI base images

Usage: guildbotics environment image <COMMAND>

Commands:
  load     Load a Docker save or OCI image archive
  list     List eligible local base images
  declare  Declare the image's digest per architecture in the selected workspace
  help     Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics environment image load

```text
Load a Docker save or OCI image archive

Usage: guildbotics environment image load [OPTIONS] <ARCHIVE>

Arguments:
  <ARCHIVE>


Options:
      --tag <TAG>
          Add a reference to the archive's own tags

      --ci
          Also distribute the archive to CI runners of its CPU architecture

  -h, --help
          Print help
```

## guildbotics environment image list

```text
List eligible local base images

Usage: guildbotics environment image list [OPTIONS]

Options:
      --format <FORMAT>
          Select the output format

          [default: markdown]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics environment image declare

```text
Declare the image's digest per architecture in the selected workspace

Usage: guildbotics environment image declare [OPTIONS] [REFERENCE]

Arguments:
  [REFERENCE]


Options:
      --default
          Restore the GuildBotics default image

      --digest <ARCH=DIGEST>
          Set the image digest for an architecture. Repeatable

  -h, --help
          Print help
```

## guildbotics server

```text
Run the server

Usage: guildbotics server <COMMAND>

Commands:
  hub-data  Back up, restore, check, upgrade, or reindex the Hub's records
  start     Run the server in the foreground until SIGINT or SIGTERM (a first SIGINT waits for the commands running, SIGTERM or a second SIGINT cancels them): GuildBotics on GUILDBOTICS_SERVER_PORT (default 7878) with its records under GUILDBOTICS_HOME (default ~/.guildbotics), and the code host on the same listener, with its data in BRANVIL_DATA_DIR (default GUILDBOTICS_HOME/data/code-host, created at the first start)
  stop      Stop the server and wait for its work to end
  help      Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics server hub-data

```text
Back up, restore, check, upgrade, or reindex the Hub's records

Usage: guildbotics server hub-data <COMMAND>

Commands:
  backup   Write a backup of the running server's records into a new or empty directory
  restore  Restore a backup into a new or empty directory and check it; check and upgrade take the result as --data-dir
  check    Check the records; a passing check lets the server open an upgraded code host database
  upgrade  Upgrade the records from the previous version, then check them (stop the server and take a backup first)
  reindex  Rebuild the code host's indexes from its records
  help     Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics server hub-data backup

```text
Write a backup of the running server's records into a new or empty directory

Usage: guildbotics server hub-data backup --to <TO>

Options:
      --to <TO>
          Directory to fill, on this machine

  -h, --help
          Print help
```

## guildbotics server hub-data restore

```text
Restore a backup into a new or empty directory and check it; check and upgrade take the result as --data-dir

Usage: guildbotics server hub-data restore --from <FROM> --data-dir <DATA_DIR>

Options:
      --from <FROM>
          Backup directory written by `server hub-data backup`

      --data-dir <DATA_DIR>
          Directory to fill

  -h, --help
          Print help
```

## guildbotics server hub-data check

```text
Check the records; a passing check lets the server open an upgraded code host database

Usage: guildbotics server hub-data check [OPTIONS]

Options:
      --data-dir <DATA_DIR>
          A restored backup instead of this machine's records

  -h, --help
          Print help
```

## guildbotics server hub-data upgrade

```text
Upgrade the records from the previous version, then check them (stop the server and take a backup first)

Usage: guildbotics server hub-data upgrade [OPTIONS]

Options:
      --data-dir <DATA_DIR>
          A restored backup instead of this machine's records

  -h, --help
          Print help
```

## guildbotics server hub-data reindex

```text
Rebuild the code host's indexes from its records

Usage: guildbotics server hub-data reindex [OPTIONS]

Options:
      --data-dir <DATA_DIR>
          A restored backup instead of this machine's records

  -h, --help
          Print help
```

## guildbotics server start

```text
Run the server in the foreground until SIGINT or SIGTERM (a first SIGINT waits for the commands running, SIGTERM or a second SIGINT cancels them): GuildBotics on GUILDBOTICS_SERVER_PORT (default 7878) with its records under GUILDBOTICS_HOME (default ~/.guildbotics), and the code host on the same listener, with its data in BRANVIL_DATA_DIR (default GUILDBOTICS_HOME/data/code-host, created at the first start)

Usage: guildbotics server start [OPTIONS]

Options:
      --services
          Start services even when automatic startup is disabled

  -h, --help
          Print help
```

## guildbotics server stop

```text
Stop the server and wait for its work to end

Usage: guildbotics server stop [OPTIONS]

Options:
      --force
          Cancel in-flight work and force termination if teardown takes too long

  -h, --help
          Print help
```

## guildbotics member

```text
Operate as a configured member through the running server

Usage: guildbotics member [ARGUMENTS]...

Arguments:
  [ARGUMENTS]...
          Member subcommand and its options
```

On the Hub, start the GuildBotics server before running member commands; use `member --workspace <path>` to verify the selected local workspace. On another machine, forward the Hub's port and run `guildbotics connect` first; the Hub selects the workspace.
See the [member capability reference](../guildbotics/templates/member/reference.md) for examples and option semantics.

```text
Operate as a configured GuildBotics member.

Usage: guildbotics member [OPTIONS]
  chat  Chat identity, posting, replies, reactions, and run completion.
  checks  Await CI checks or retry a job.
  context  Show non-secret member context.
  git  Read repositories and prepare, commit, push, and publish member workspaces.
  help  Print the member capability reference (commands and cross-cutting rules). This is the same reference embedded in ``member context``; use it to reread the available commands without re-running the full context.
  memory  Record, recall, and maintain member memory documents.
  pr  Read, open, update, comment on, and review pull requests, react to their comments and reviews, and download their CI artifacts.
  repo-info  Read information about the repository as a whole.
  task  Completion records of runs other than a chat event's.
  ticket  Read, create, update, and comment on tickets, react to their comments, and download their attachments.
  --workspace <PATH>  Require the server to have this workspace selected
  --help  Print help
```

### guildbotics member chat

```text
Chat identity, posting, replies, reactions, and run completion.

Usage: guildbotics member chat [OPTIONS]
  complete  Finish the chat workflow run on its event with evidence.
  identity  Show the member's chat identity.
  inspect  Inspect chat channel or thread messages for interactive decisions.
  noop  Record a deliberate no-op for the chat run's event.
  post  Post a new channel message.
  reaction  Chat reaction operations.
  reply  Reply in a thread in the member voice.
  resolve-channel  Resolve a channel name to its channel id without reading or posting; the id is empty when no channel has that name.
  updates  Check the source thread's durable queue before replying or publishing in a chat workflow; reconsider new messages, and stop external writes when reception is unavailable.
  --help  Print help
```

### guildbotics member chat complete

```text
Finish the chat workflow run on its event with evidence.

Usage: guildbotics member chat complete [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --status <done|asking|blocked>  Run outcome. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat identity

```text
Show the member's chat identity.

Usage: guildbotics member chat identity [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --format <json|markdown> [default: markdown]  Output format.
  --help  Print help
```

### guildbotics member chat inspect

```text
Inspect chat channel or thread messages for interactive decisions.

Usage: guildbotics member chat inspect [OPTIONS]
  channel  Read recent channel messages.
  thread  Read a thread before replying or reacting.
  --help  Print help
```

### guildbotics member chat inspect channel

```text
Read recent channel messages.

Usage: guildbotics member chat inspect channel [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-id <VALUE> [default: ]  Channel id of the target channel.
  --channel-name <VALUE> [default: ]  Channel name (alternative to --channel-id).
  --since <VALUE>  Only include messages that occurred at or after this time (ISO 8601; local time without an offset).
  --until <VALUE>  Only include messages that occurred at or before this time (ISO 8601; local time without an offset).
  --limit <VALUE> [default: 50; 1<=x<=200]  Maximum number of messages.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat inspect thread

```text
Read a thread before replying or reacting.

Usage: guildbotics member chat inspect thread [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-id <VALUE> [default: ]  Channel id of the target channel.
  --channel-name <VALUE> [default: ]  Channel name (alternative to --channel-id).
  --thread-id <VALUE> [default: ]  Thread id (with --channel-id).
  --message-url <VALUE> [default: ]  Chat message URL (alternative to --channel-id with --thread-id).
  --limit <VALUE> [default: 100; 1<=x<=200]  Maximum number of messages.
  --format <json|markdown> [default: json]  Output format.
  --cursor <VALUE>  Return messages before this opaque position.
  --help  Print help
```

### guildbotics member chat noop

```text
Record a deliberate no-op for the chat run's event.

Usage: guildbotics member chat noop [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat post

```text
Post a new channel message.

Usage: guildbotics member chat post [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-id <VALUE> [default: ]  Channel id of the target channel.
  --channel-name <VALUE> [default: ]  Channel name (alternative to --channel-id).
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat reaction

```text
Chat reaction operations.

Usage: guildbotics member chat reaction [OPTIONS]
  add  Add a semantic reaction.
  --help  Print help
```

### guildbotics member chat reaction add

```text
Add a semantic reaction.

Usage: guildbotics member chat reaction add [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-id <VALUE> [default: ]  Channel id of the target channel.
  --channel-name <VALUE> [default: ]  Channel name (alternative to --channel-id).
  --message-id <VALUE> [default: ]  Message id (with --channel-id).
  --message-url <VALUE> [default: ]  Chat message URL (alternative to --channel-id with --message-id).
  --reaction <ack|agree|celebrate|support>  Semantic reaction to add. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat reply

```text
Reply in a thread in the member voice.

Usage: guildbotics member chat reply [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-id <VALUE> [default: ]  Channel id of the target channel.
  --channel-name <VALUE> [default: ]  Channel name (alternative to --channel-id).
  --thread-id <VALUE> [default: ]  Thread id (with --channel-id).
  --message-url <VALUE> [default: ]  Chat message URL (alternative to --channel-id with --thread-id).
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat resolve-channel

```text
Resolve a channel name to its channel id without reading or posting; the id is empty when no channel has that name.

Usage: guildbotics member chat resolve-channel [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --channel-name <VALUE>  Channel name to resolve. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member chat updates

```text
Check the source thread's durable queue before replying or publishing in a chat workflow; reconsider new messages, and stop external writes when reception is unavailable.

Usage: guildbotics member chat updates [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member checks

```text
Await CI checks or retry a job.

Usage: guildbotics member checks [OPTIONS]
  await  End any run -- a ticket's, a chat thread's, or a command's -- to await its own PRs' pending checks: a notice on each PR, from which the patrol resumes the run's work once they settle, and the resumed run carries on with any of that work still unfinished.
  retry  Retry a CI job by its page URL.
  --help  Print help
```

### guildbotics member checks await

```text
End any run -- a ticket's, a chat thread's, or a command's -- to await its own PRs' pending checks: a notice on each PR, from which the patrol resumes the run's work once they settle, and the resumed run carries on with any of that work still unfinished.

Usage: guildbotics member checks await [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member checks retry

```text
Retry a CI job by its page URL.

Usage: guildbotics member checks retry [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --job-url <VALUE>  Page URL of the CI job to retry. [required]
  --runner <VALUE>  Run this pull request check on the named runner.
  --exclude-runner <VALUE>  Exclude the named runner from this pull request check. (repeatable)
  --format <json|text> [default: json]  Output format.
  --help  Print help
```

### guildbotics member context

```text
Show non-secret member context.

Usage: guildbotics member context [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --check-credentials  Also verify the member's provider credentials.
  --format <json|markdown> [default: markdown]  Output format.
  --help  Print help
```

### guildbotics member git

```text
Read repositories and prepare, commit, push, and publish member workspaces.

Usage: guildbotics member git [OPTIONS]
  commit  Commit already-staged changes with the member identity. Stage the files you want with plain git (e.g. ``git add``) first; this command commits only what is staged and applies the member name/email to that single commit without changing the repository's git config.
  prepare  Clone/checkout an isolated member workspace: a ticket branch `ticket/<key>` (--issue-url; a --repo given with it must name the ticket's repository), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo --branch). A repository is named by its path. Unfinished checkouts stay assigned to their work and requested branch; idle checkouts of the same repository are reused with their build caches. Call prepare every run and use the returned repo_path and branch: a path from an earlier run may now belong to another work. Unfinished Git state is kept, including an agent-created branch; an empty branch means detached HEAD. Member workspaces are worked on only inside a running GuildBotics command, whose isolated environment runs every git of them.
  publish  Commit already-staged changes with the member identity, then push. Stage the files you want with plain git (e.g. ``git add``) first; this commits only what is staged with the member name/email and pushes the branch using the member credential.
  push  Push the current branch with the member credential and report required checks for matching open PRs.
  show  Read literal paths on the code host's current default branch, without a checkout. Files return UTF-8 content; directories (including '.') return immediate entry names and kinds. Missing paths return kind missing; binary or oversized objects omit content with status binary or too_large. Results include default_branch and commit; output is bounded by the member result limit. Uses the member credential on the host, both interactively and inside a command, without a lease or approval.
  --help  Print help
```

### guildbotics member git commit

```text
Commit already-staged changes with the member identity. Stage the files you want with plain git (e.g. ``git add``) first; this command commits only what is staged and applies the member name/email to that single commit without changing the repository's git config.

Usage: guildbotics member git commit [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --repo-path <VALUE>  Path to the member repository workspace. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --workspace-mode <member|current> [default: member]  Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member git prepare

```text
Clone/checkout an isolated member workspace: a ticket branch `ticket/<key>` (--issue-url; a --repo given with it must name the ticket's repository), a PR head (--pr-url, alone or together with --issue-url, which checks out the PR head), or an ad-hoc branch (--repo --branch). A repository is named by its path. Unfinished checkouts stay assigned to their work and requested branch; idle checkouts of the same repository are reused with their build caches. Call prepare every run and use the returned repo_path and branch: a path from an earlier run may now belong to another work. Unfinished Git state is kept, including an agent-created branch; an empty branch means detached HEAD. Member workspaces are worked on only inside a running GuildBotics command, whose isolated environment runs every git of them.

Usage: guildbotics member git prepare [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --issue-url <VALUE> [default: ]  Ticket URL to prepare a workspace for.
  --pr-url <VALUE> [default: ]  PR URL whose head branch to check out.
  --repo <VALUE> [default: ]  Repository path (for example, owner/repo; follow the service's path format): with --branch, or with --issue-url, where it must name the ticket's repository.
  --branch <VALUE> [default: ]  Branch to create or check out (with --repo).
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member git publish

```text
Commit already-staged changes with the member identity, then push. Stage the files you want with plain git (e.g. ``git add``) first; this commits only what is staged with the member name/email and pushes the branch using the member credential.

Usage: guildbotics member git publish [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --repo-path <VALUE>  Path to the member repository workspace. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --workspace-mode <member|current> [default: member]  Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member git push

```text
Push the current branch with the member credential and report required checks for matching open PRs.

Usage: guildbotics member git push [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --repo-path <VALUE>  Path to the member repository workspace. [required]
  --workspace-mode <member|current> [default: member]  Use 'member' for isolated workflow workspaces or 'current' for the repository currently open in an interactive coding session.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member git show

```text
Read literal paths on the code host's current default branch, without a checkout. Files return UTF-8 content; directories (including '.') return immediate entry names and kinds. Missing paths return kind missing; binary or oversized objects omit content with status binary or too_large. Results include default_branch and commit; output is bounded by the member result limit. Uses the member credential on the host, both interactively and inside a command, without a lease or approval.

Usage: guildbotics member git show [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --repo <VALUE>  Repository path of the code host. [required]
  --path <VALUE>  Literal repository-relative path on the default branch; repeat to read several. Use '.' to list the root. [required] (repeatable)
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member help

```text
Print the member capability reference (commands and cross-cutting rules). This is the same reference embedded in ``member context``; use it to reread the available commands without re-running the full context.

Usage: guildbotics member help [OPTIONS]
  --help  Print help
```

### guildbotics member memory

```text
Record, recall, and maintain member memory documents.

Usage: guildbotics member memory [OPTIONS]
  archive  Move a stale memory under archived/ and remove it from recall and digest.
  get  Read one memory document's metadata and body without changing recency.
  promote  Move a personal memory into team memory without changing the document id.
  recall  Search personal and team memory by literal OR queries and return compact hits.
  record  Create a memory document and move it to the front of the digest.
  touch  Mark a useful memory as actually used by moving it to the digest front.
  update  Replace selected body or metadata fields and move the document to the digest front.
  --help  Print help
```

### guildbotics member memory archive

```text
Move a stale memory under archived/ and remove it from recall and digest.

Usage: guildbotics member memory archive [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --id <VALUE>  Memory document id. [required]
  --team  Operate on team memory instead of personal memory.
  --policy-approved  Confirm that a human approved this policy memory change.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory get

```text
Read one memory document's metadata and body without changing recency.

Usage: guildbotics member memory get [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --id <VALUE>  Memory document id. [required]
  --team  Operate on team memory instead of personal memory.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory promote

```text
Move a personal memory into team memory without changing the document id.

Usage: guildbotics member memory promote [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --id <VALUE>  Memory document id. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory recall

```text
Search personal and team memory by literal OR queries and return compact hits.

Usage: guildbotics member memory recall [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --query <VALUE>  Literal search query; repeat for OR matching. (repeatable)
  --meta-only  Return hit metadata without body excerpts.
  --limit <VALUE> [default: 20; 1<=x<=200]  Maximum number of hits.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory record

```text
Create a memory document and move it to the front of the digest.

Usage: guildbotics member memory record [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --scope <personal|team> [default: personal]  Store as personal or team memory.
  --title <VALUE>  Document title. [required]
  --summary <VALUE> [default: ]  One-line summary shown in recall hits and the digest.
  --keyword <VALUE>  Recall keyword. May be repeated. (repeatable)
  --ticket <VALUE>  Related ticket URL (source anchor). May be repeated. (repeatable)
  --pr <VALUE>  Related PR URL (source anchor). May be repeated. (repeatable)
  --channel <VALUE>  Related chat channel URL (source anchor). May be repeated. (repeatable)
  --thread <VALUE>  Related chat thread URL (source anchor). May be repeated. (repeatable)
  --kind <note|policy> [default: note]  Document kind; 'policy' requires --policy-approved.
  --pin  Pin as a standing rule included in member context.
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --policy-approved  Confirm that a human approved this policy memory change.
  --set <VALUE>  Extra metadata as key=value. May be repeated. (repeatable)
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory touch

```text
Mark a useful memory as actually used by moving it to the digest front.

Usage: guildbotics member memory touch [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --id <VALUE>  Memory document id. [required]
  --team  Operate on team memory instead of personal memory.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member memory update

```text
Replace selected body or metadata fields and move the document to the digest front.

Usage: guildbotics member memory update [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --id <VALUE>  Memory document id. [required]
  --team  Operate on team memory instead of personal memory.
  --title <VALUE>  New document title.
  --summary <VALUE>  New one-line summary.
  --keyword <VALUE>  Replace all recall keywords. May be repeated. (repeatable)
  --add-keyword <VALUE>  Add a recall keyword. May be repeated. (repeatable)
  --remove-keyword <VALUE>  Remove a recall keyword. May be repeated. (repeatable)
  --ticket <VALUE>  Related ticket URL (source anchor). May be repeated. (repeatable)
  --pr <VALUE>  Related PR URL (source anchor). May be repeated. (repeatable)
  --channel <VALUE>  Related chat channel URL (source anchor). May be repeated. (repeatable)
  --thread <VALUE>  Related chat thread URL (source anchor). May be repeated. (repeatable)
  --pin  Pin as a standing rule included in member context.
  --unpin  Remove the pin.
  --kind <note|policy>  Change the document kind; 'policy' requires --policy-approved.
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --policy-approved  Confirm that a human approved this policy memory change.
  --set <VALUE>  Extra metadata as key=value. May be repeated. (repeatable)
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr

```text
Read, open, update, comment on, and review pull requests, react to their comments and reviews, and download their CI artifacts.

Usage: guildbotics member pr [OPTIONS]
  artifact  CI artifact operations.
  comment  Comment on a PR conversation.
  create  Open a PR, or return the existing open PR for the same head and base branches. --issue-url, which may be repeated, links each ticket in the PR body as a reference, or with --closes-issue so that merging the PR into the default branch closes every one of them; --closes-issue refuses a ticket kept outside this code host, which a merge cannot close.
  reaction  Reactions to pull request comments and reviews.
  read  Read one bounded page of a PR's resource: pull_requests, pull_request_comments (conversation comments), pull_request_reviews, pull_request_files, pull_request_threads (each with all of its comments), or pull_request_readiness. Collections accept page_size (1-100). Readiness accepts failed_logs (boolean) and log_tail_bytes (1-65536, default 65536). Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.
  reply  Reply to an inline review thread.
  review  Submit a review verdict as a review on the head the member read (`head_sha` of the PR's read); a conversation comment does not consume a review request or make the member a reviewer. When the head has moved, nothing is written: read the new commits, then submit again on the new head.
  review-comment  Create a new inline review comment on a diff line of the head the member read; when the head has moved, read the new commits and comment again.
  update  Change a PR's body, title, or ticket links. --issue-url replaces all links with those tickets; --closes-issue closes all of them on merge into the default branch and --refs-issue only refers (the default). --drop-issue-links removes all links without requiring a body. Without either option, body replacement preserves existing ticket links and ignores new managed link lines in the body.
  --help  Print help
```

### guildbotics member pr artifact

```text
CI artifact operations.

Usage: guildbotics member pr artifact [OPTIONS]
  download  Download and extract a size-limited CI artifact of the PR's head; remove repository files after inspection.
  --help  Print help
```

### guildbotics member pr artifact download

```text
Download and extract a size-limited CI artifact of the PR's head; remove repository files after inspection.

Usage: guildbotics member pr artifact download [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --name <VALUE>  Exact artifact name. [required]
  --dest <VALUE> [default: .]  Directory to extract into. Defaults to the current directory. Remove downloaded files after inspection when this is inside a repository.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr comment

```text
Comment on a PR conversation.

Usage: guildbotics member pr comment [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr create

```text
Open a PR, or return the existing open PR for the same head and base branches. --issue-url, which may be repeated, links each ticket in the PR body as a reference, or with --closes-issue so that merging the PR into the default branch closes every one of them; --closes-issue refuses a ticket kept outside this code host, which a merge cannot close.

Usage: guildbotics member pr create [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --repo <VALUE>  Target repository path (for example, owner/repo; follow the service's path format). [required]
  --head <VALUE>  Head branch containing the changes. [required]
  --base <VALUE> [default: ]  Base branch for the pull request. Defaults to the repository default branch.
  --title <VALUE>  Pull request title. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --issue-url <VALUE>  Related ticket URL to link to the PR. May be repeated. (repeatable)
  --closes-issue/--refs-issue [default: false]  Link every --issue-url ticket so that merging the PR closes it, or only refer to it. Defaults to referring; --closes-issue refuses a ticket kept outside this code host.
  --draft <true|false> [default: false]  Open the pull request as a draft.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr reaction

```text
Reactions to pull request comments and reviews.

Usage: guildbotics member pr reaction [OPTIONS]
  add  React to a conversation comment, a review thread comment, or a review's body of the PR, by the id its read gave it.
  --help  Print help
```

### guildbotics member pr reaction add

```text
React to a conversation comment, a review thread comment, or a review's body of the PR, by the id its read gave it.

Usage: guildbotics member pr reaction add [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --comment-id <VALUE>  The id the item's read gave the comment (or review) to react to. [required]
  --reaction <ack|agree|celebrate|support>  Semantic reaction to add. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr read

```text
Read one bounded page of a PR's resource: pull_requests, pull_request_comments (conversation comments), pull_request_reviews, pull_request_files, pull_request_threads (each with all of its comments), or pull_request_readiness. Collections accept page_size (1-100). Readiness accepts failed_logs (boolean) and log_tail_bytes (1-65536, default 65536). Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

Usage: guildbotics member pr read [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --resource <pull_requests|pull_request_comments|pull_request_reviews|pull_request_files|pull_request_threads|pull_request_readiness>  Resource to read; see member help for its conditions. [required]
  --subject <VALUE>  What the resource is part of: the ticket's or pull request's URL, or the tracker/repository path (tracker_tickets searches a tracker). [required]
  --params <VALUE> [default: ]  Resource conditions as a JSON object.
  --continuation <VALUE> [default: ]  Continuation returned for the same resource and conditions.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr reply

```text
Reply to an inline review thread.

Usage: guildbotics member pr reply [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --reply-target-id <VALUE>  reply_target_id from repository/pr_inspect include_comments=true. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr review

```text
Submit a review verdict as a review on the head the member read (`head_sha` of the PR's read); a conversation comment does not consume a review request or make the member a reviewer. When the head has moved, nothing is written: read the new commits, then submit again on the new head.

Usage: guildbotics member pr review [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --event <approve|request_changes|comment>  Review verdict submitted on the head --head names. [required]
  --head <VALUE>  The head commit (head_sha) the member read; refused when the PR's head has moved since. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr review-comment

```text
Create a new inline review comment on a diff line of the head the member read; when the head has moved, read the new commits and comment again.

Usage: guildbotics member pr review-comment [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --head <VALUE>  The head commit (head_sha) the member read; refused when the PR's head has moved since. [required]
  --path <VALUE>  File path in the PR diff. [required]
  --line <VALUE> [x>=1]  Line number on the chosen diff side. [x\>=1; required] [required]
  --side <LEFT|RIGHT> [default: RIGHT]  Diff side of the line.
  --start-line <VALUE> [x>=1]  Start line for a multi-line comment. [x\>=1]
  --start-side <LEFT|RIGHT>  Diff side of --start-line.
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member pr update

```text
Change a PR's body, title, or ticket links. --issue-url replaces all links with those tickets; --closes-issue closes all of them on merge into the default branch and --refs-issue only refers (the default). --drop-issue-links removes all links without requiring a body. Without either option, body replacement preserves existing ticket links and ignores new managed link lines in the body.

Usage: guildbotics member pr update [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Pull request URL. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --title <VALUE>  Replace the pull request title.
  --issue-url <VALUE>  Replace all PR ticket links with these URLs. May be repeated. (repeatable)
  --closes-issue/--refs-issue [default: false]  Close every --issue-url ticket on merge, or only refer to it (default).
  --drop-issue-links  Remove all PR ticket links. Cannot be combined with --issue-url.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member repo-info

```text
Read information about the repository as a whole.

Usage: guildbotics member repo-info [OPTIONS]
  read  Read one bounded page of the repository's dependency_alerts, the repository named by its path. It accepts state (open by default, resolved, dismissed) and page_size (1-100, default 30). With an id condition, it returns that one alert and accepts no other condition or continuation. For formatted or JSON alerts, run repository/security_alerts in a ready command environment. Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.
  --help  Print help
```

### guildbotics member repo-info read

```text
Read one bounded page of the repository's dependency_alerts, the repository named by its path. It accepts state (open by default, resolved, dismissed) and page_size (1-100, default 30). With an id condition, it returns that one alert and accepts no other condition or continuation. For formatted or JSON alerts, run repository/security_alerts in a ready command environment. Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

Usage: guildbotics member repo-info read [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --resource <dependency_alerts>  Resource to read; see member help for its conditions. [required]
  --subject <VALUE>  What the resource is part of: the ticket's or pull request's URL, or the tracker/repository path (tracker_tickets searches a tracker). [required]
  --params <VALUE> [default: ]  Resource conditions as a JSON object.
  --continuation <VALUE> [default: ]  Continuation returned for the same resource and conditions.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member task

```text
Completion records of runs other than a chat event's.

Usage: guildbotics member task [OPTIONS]
  complete  Finish a run other than a chat event's with evidence, checking required head checks before accepting done. For asking/blocked, use the context roster: name the next member to act by person_id and address them on the item with their service mention spelling. The CLI validates whether their patrol receives any item you commented on. Human members are recorded and notified; omit only for people outside the team. Without a recipient, the CLI says the owner will be notified.
  status  Inspect the run's recorded evidence.
  --help  Print help
```

### guildbotics member task complete

```text
Finish a run other than a chat event's with evidence, checking required head checks before accepting done. For asking/blocked, use the context roster: name the next member to act by person_id and address them on the item with their service mention spelling. The CLI validates whether their patrol receives any item you commented on. Human members are recorded and notified; omit only for people outside the team. Without a recipient, the CLI says the owner will be notified.

Usage: guildbotics member task complete [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --waiting-for <VALUE>  Next member to act, by the context roster's person_id (repeatable; asking/blocked only). (repeatable)
  --status <done|asking|blocked>  Run outcome. [required]
  --work-remains  Ticket work remains after this run (done ticket work only); resume after linked PRs merge.
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member task status

```text
Inspect the run's recorded evidence.

Usage: guildbotics member task status [OPTIONS]
  --person <VALUE> [default: ]  Person ID or name of the member.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket

```text
Read, create, update, and comment on tickets, react to their comments, and download their attachments.

Usage: guildbotics member ticket [OPTIONS]
  attachment  Attachment operations.
  comment  Comment on a ticket in the member voice.
  create  Open a follow-up ticket a human asked for, in the repository a ticket's read names (`repository`); every ticket is on the board. Creation settings are covered by the human's approval. The lane defaults to ready with a parent or blockers, new otherwise; interactive work starting now uses --interactive --assignee <person_id> --lane in_progress.
  reaction  Reactions to ticket comments.
  read  Read one bounded page of a ticket's resource: tickets (the ticket itself: key, repository, author (creator's account name), assignee, lane, interactive, interactive_ancestor, parent, children, blocked_by, blocks, ...), ticket_comments, ticket_attachments (the files its body and comments refer to: id, name, media_type, size, and referenced_by, `body` or a comment id), or ticket_pull_requests (the code host's pull requests that refer to it), or tracker_tickets (subject: the repository path the tickets are kept in; conditions: text with all whitespace-separated terms in title/body, state open|closed|all, label, lane new|ready|in_progress|done, page_size; lane returns board order; items are tickets as `tickets` returns them). Collections accept page_size (1-100). Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.
  update  Change a ticket's body, title, labels, state, interactive mode, parent, or the tickets it is blocked by; empty content removes the body, and an empty `--parent` the parent. Changing interactive mode requires a human's request or approval for that ticket (--human-approved). Reads and updates return the ticket's own interactive setting and its interactive_ancestor; clearing the setting does not override an interactive ancestor. Report that inherited state to the requester. 対話指定の切り替えは、そのチケットについて人が依頼・承認した場合だけ --human-approved を付けて行う。継承元がある間は対話のままなので、その結果を伝える。 Use --before or --after to apply a human-approved order within the same tracker and lane, in interactive sessions only. In interactive sessions, --assignee assigns the ticket in place of its assignee and --lane in_progress moves the ticket; both require --human-approved. Updates return the ticket.
  --help  Print help
```

### guildbotics member ticket attachment

```text
Attachment operations.

Usage: guildbotics member ticket attachment [OPTIONS]
  download  Download a size-limited file the ticket refers to, by the id its ticket_attachments read gave it, as a file of the destination named as it was attached; remove repository files after reading.
  --help  Print help
```

### guildbotics member ticket attachment download

```text
Download a size-limited file the ticket refers to, by the id its ticket_attachments read gave it, as a file of the destination named as it was attached; remove repository files after reading.

Usage: guildbotics member ticket attachment download [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Ticket URL. [required]
  --id <VALUE>  The id the ticket's ticket_attachments read gave the attachment. [required]
  --dest <VALUE> [default: .]  Directory to write the file into. Defaults to the current directory. Remove downloaded files after reading when this is inside a repository.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket comment

```text
Comment on a ticket in the member voice.

Usage: guildbotics member ticket comment [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Ticket URL. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket create

```text
Open a follow-up ticket a human asked for, in the repository a ticket's read names (`repository`); every ticket is on the board. Creation settings are covered by the human's approval. The lane defaults to ready with a parent or blockers, new otherwise; interactive work starting now uses --interactive --assignee <person_id> --lane in_progress.

Usage: guildbotics member ticket create [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --tracker <VALUE>  Where the ticket is kept: a repository path (for example, owner/repo; follow the service's path format). [required]
  --title <VALUE>  Ticket title. [required]
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --content-stdin  Read the command's entire free-form content from standard input.
  --label <VALUE>  Label already defined in the tracker. Repeat for several labels. (repeatable)
  --interactive  Create for interactive work.
  --assignee <VALUE>  Assignee's person_id from the member context roster, including humans.
  --lane <new|ready|in_progress>  Initial lane; defaults to ready with a parent or blockers, new otherwise.
  --parent <VALUE>  Parent ticket URL.
  --blocked-by <VALUE>  Blocking ticket URL. Repeat for several blockers. (repeatable)
  --human-approved  Confirm that a human instructed or approved this change.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket reaction

```text
Reactions to ticket comments.

Usage: guildbotics member ticket reaction [OPTIONS]
  add  React to a comment of the ticket, by the id its read gave it.
  --help  Print help
```

### guildbotics member ticket reaction add

```text
React to a comment of the ticket, by the id its read gave it.

Usage: guildbotics member ticket reaction add [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Ticket URL. [required]
  --comment-id <VALUE>  The id the item's read gave the comment (or review) to react to. [required]
  --reaction <ack|agree|celebrate|support>  Semantic reaction to add. [required]
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket read

```text
Read one bounded page of a ticket's resource: tickets (the ticket itself: key, repository, author (creator's account name), assignee, lane, interactive, interactive_ancestor, parent, children, blocked_by, blocks, ...), ticket_comments, ticket_attachments (the files its body and comments refer to: id, name, media_type, size, and referenced_by, `body` or a comment id), or ticket_pull_requests (the code host's pull requests that refer to it), or tracker_tickets (subject: the repository path the tickets are kept in; conditions: text with all whitespace-separated terms in title/body, state open|closed|all, label, lane new|ready|in_progress|done, page_size; lane returns board order; items are tickets as `tickets` returns them). Collections accept page_size (1-100). Results contain the items and continuation; reuse identical conditions for the next page. HTTP methods, headers, and GraphQL are not accepted.

Usage: guildbotics member ticket read [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --resource <tracker_tickets|tickets|ticket_comments|ticket_attachments|ticket_pull_requests>  Resource to read; see member help for its conditions. [required]
  --subject <VALUE>  What the resource is part of: the ticket's or pull request's URL, or the tracker/repository path (tracker_tickets searches a tracker). [required]
  --params <VALUE> [default: ]  Resource conditions as a JSON object.
  --continuation <VALUE> [default: ]  Continuation returned for the same resource and conditions.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

### guildbotics member ticket update

```text
Change a ticket's body, title, labels, state, interactive mode, parent, or the tickets it is blocked by; empty content removes the body, and an empty `--parent` the parent. Changing interactive mode requires a human's request or approval for that ticket (--human-approved). Reads and updates return the ticket's own interactive setting and its interactive_ancestor; clearing the setting does not override an interactive ancestor. Report that inherited state to the requester. 対話指定の切り替えは、そのチケットについて人が依頼・承認した場合だけ --human-approved を付けて行う。継承元がある間は対話のままなので、その結果を伝える。 Use --before or --after to apply a human-approved order within the same tracker and lane, in interactive sessions only. In interactive sessions, --assignee assigns the ticket in place of its assignee and --lane in_progress moves the ticket; both require --human-approved. Updates return the ticket.

Usage: guildbotics member ticket update [OPTIONS]
  --person <VALUE>  Person ID or name of the member. [required]
  --url <VALUE>  Ticket URL. [required]
  --before <VALUE>  Place this ticket immediately before the given ticket in the same tracker and lane; requires --human-approved and an interactive session.
  --after <VALUE>  Place this ticket immediately after the given ticket in the same tracker and lane; requires --human-approved and an interactive session.
  --content-file <VALUE>  Read the command's entire free-form content from a UTF-8 file.
  --assignee <VALUE>  Assign the ticket to this roster person_id in place of its assignee; interactive sessions only, requires --human-approved.
  --lane <in_progress>  Move to in_progress; interactive sessions only, requires --human-approved.
  --content-stdin  Read the command's entire free-form content from standard input.
  --title <VALUE>  Replace the ticket title.
  --interactive/--no-interactive  Set or clear this ticket's interactive mode; requires --human-approved. An interactive ancestor still keeps it interactive.
  --add-label <VALUE>  Add a label already defined in the tracker. Repeat for several labels. (repeatable)
  --remove-label <VALUE>  Remove a label from the ticket. Repeat for several labels. (repeatable)
  --state <open|closed>  Close or reopen the ticket; requires --human-approved.
  --state-reason <completed|not_planned>  Why the ticket is closed; requires --state closed.
  --parent <VALUE>  URL of the ticket this one becomes a part of; "" makes it a part of none.
  --add-blocked-by <VALUE>  URL of a ticket that must be closed before this one is started. Repeat for several tickets. (repeatable)
  --remove-blocked-by <VALUE>  URL of a ticket this one is no longer blocked by. Repeat for several tickets. (repeatable)
  --human-approved  Confirm that a human instructed or approved this change.
  --format <json|markdown> [default: json]  Output format.
  --help  Print help
```

## guildbotics git-credential

```text
Supply Git with this machine's credential; issuing a token requires the server

Usage: guildbotics git-credential <ACTION>

Arguments:
  <ACTION>
          Credential helper action from Git

Options:
  -h, --help
          Print help
```

## guildbotics install

```text
Install this executable and the GuildBotics skill for this user

Usage: guildbotics install

Options:
  -h, --help
          Print help
```

## guildbotics workspace

```text
Select the active workspace for the server and AI CLI tools

Usage: guildbotics workspace <COMMAND>

Commands:
  use      Persist the active workspace for the server and AI CLI tools
  current  Show the persisted active workspace
  status   Show the active workspace status, even when no workspace is configured
  help     Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics workspace use

```text
Persist the active workspace for the server and AI CLI tools

Usage: guildbotics workspace use [OPTIONS] <WORKSPACE_DIR>

Arguments:
  <WORKSPACE_DIR>


Options:
      --format <FORMAT>
          Select the output format

          [default: markdown]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics workspace current

```text
Show the persisted active workspace

Usage: guildbotics workspace current [OPTIONS]

Options:
      --format <FORMAT>
          Select the output format

          [default: markdown]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics workspace status

```text
Show the active workspace status, even when no workspace is configured

Usage: guildbotics workspace status [OPTIONS]

Options:
      --format <FORMAT>
          Select the output format

          [default: markdown]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics run

```text
Run a command on the server in the selected workspace's isolated environment

Usage: guildbotics run [OPTIONS] [COMMAND] [ARGS]...

Arguments:
  [COMMAND]
          Command name, optionally followed by @PERSON

  [ARGS]...
          Arguments for the command

Options:
      --person <PERSON>
          Member ID or name. Overrides a member specified as COMMAND@PERSON

      --cwd <CWD>
          Working directory for the command

      --resume <RESUME>
          Retrieve a prior run by ID without executing it again

  -h, --help
          Print help
```

## guildbotics diagnostics

```text
Read a workspace's recorded diagnostics

Usage: guildbotics diagnostics [OPTIONS] <COMMAND>

Commands:
  traces  List recorded executions, newest first
  trace   Show an execution's summary and records
  system  Show service-wide records outside an execution
  help    Print this message or the help of the given subcommand(s)

Options:
      --workspace <WORKSPACE>
          Read a different workspace

      --format <FORMAT>
          Select the output format

          [default: json]
          [possible values: markdown, json]

  -h, --help
          Print help
```

## guildbotics diagnostics traces

```text
List recorded executions, newest first

Usage: guildbotics diagnostics traces [OPTIONS]

Options:
      --source <SOURCE>
          Only traces from this execution source

      --person <PERSON>
          Only traces recorded for this member

      --query <QUERY>
          Search execution summaries and transcripts

      --attr-key <ATTR_KEY>
          Only traces carrying this attribute

      --attr-value <ATTR_VALUE>
          Match this value of --attr-key

      --include-assistant
          Include the screens' AI assistant sessions

      --no-include-assistant
          Hide the screens' AI assistant sessions

      --limit <LIMIT>
          Maximum traces

          [default: 50]

  -h, --help
          Print help
```

## guildbotics diagnostics trace

```text
Show an execution's summary and records

Usage: guildbotics diagnostics trace [OPTIONS] <TRACE_ID>

Arguments:
  <TRACE_ID>


Options:
      --kind <KIND>
          Filter by record kind. Repeat to combine kinds

          [possible values: event, log, io, memory]

      --level <LEVEL>
          Filter records by log level

      --limit <LIMIT>
          Maximum records, keeping the newest

          [default: 200]

  -h, --help
          Print help
```

## guildbotics diagnostics system

```text
Show service-wide records outside an execution

Usage: guildbotics diagnostics system [OPTIONS]

Options:
      --limit <LIMIT>
          Maximum records, keeping the newest

          [default: 50]

  -h, --help
          Print help
```

## guildbotics version

```text
Print the binary's version

Usage: guildbotics version

Options:
  -h, --help
          Print help
```

## guildbotics secrets

```text
Manage workspace or shared identity secrets

Usage: guildbotics secrets [OPTIONS] <COMMAND>

Commands:
  status  Show the OS secret-store status and number of keys
  list    List the names of stored secrets
  set     Store a secret value from a file or prompt
  delete  Delete a stored secret
  export  Export secrets in dotenv format
  import  Import secrets from a dotenv file
  help    Print this message or the help of the given subcommand(s)

Options:
      --workspace <WORKSPACE>
          Use this workspace instead of the active workspace

      --identities
          Manage the identity store shared by all workspaces

  -h, --help
          Print help
```

## guildbotics secrets status

```text
Show the OS secret-store status and number of keys

Usage: guildbotics secrets status

Options:
  -h, --help
          Print help
```

## guildbotics secrets list

```text
List the names of stored secrets

Usage: guildbotics secrets list

Options:
  -h, --help
          Print help
```

## guildbotics secrets set

```text
Store a secret value from a file or prompt

Usage: guildbotics secrets set [OPTIONS] <KEY> [VALUE]

Arguments:
  <KEY>


  [VALUE]


Options:
      --from-file <FROM_FILE>
          Read the secret value from a UTF-8 file

  -h, --help
          Print help
```

## guildbotics secrets delete

```text
Delete a stored secret

Usage: guildbotics secrets delete <KEY>

Arguments:
  <KEY>


Options:
  -h, --help
          Print help
```

## guildbotics secrets export

```text
Export secrets in dotenv format

Usage: guildbotics secrets export [OPTIONS]

Options:
      --file <FILE>
          Write to an owner-only file instead of standard output

  -h, --help
          Print help
```

## guildbotics secrets import

```text
Import secrets from a dotenv file

Usage: guildbotics secrets import <FILE>

Arguments:
  <FILE>


Options:
  -h, --help
          Print help
```

## guildbotics service

```text
Start or stop scheduled commands, ticket patrol, and chat events

Usage: guildbotics service <COMMAND>

Commands:
  start  Start the services with the settings saved on the Service screen
  stop   Stop the services after their in-flight work; the server keeps running
  help   Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics service start

```text
Start the services with the settings saved on the Service screen

Usage: guildbotics service start

Options:
  -h, --help
          Print help
```

## guildbotics service stop

```text
Stop the services after their in-flight work; the server keeps running

Usage: guildbotics service stop [OPTIONS]

Options:
      --force
          Cancel in-flight work instead of waiting for it

  -h, --help
          Print help
```

## guildbotics connect

```text
Connect this machine's CLI through the server's consent screen

Usage: guildbotics connect

Options:
  -h, --help
          Print help
```

## guildbotics open

```text
Open the GuildBotics screens in the browser. The browser enters with a shared single-use link that expires after 60 seconds. The session also opens Branvil

Usage: guildbotics open [OPTIONS]

Options:
      --print
          Print the link instead of opening it, to paste into a browser on another machine

      --branvil
          Open the Branvil screens

  -h, --help
          Print help
```

## guildbotics runner

```text
Run the CI runner, a separate process that executes jobs in microVMs

Usage: guildbotics runner [OPTIONS]
       guildbotics runner <COMMAND>

Commands:
  join         Join this machine as a runner through the owner consent screen
  register     Create a runner member and print its token once (needs the owner's token)
  pull         Pull an image into the runner's image cache (jobs never pull)
  doctor       Show which msb is used and whether the executor is ready
  install-msb  Copy an msb binary (and the libkrunfw next to it) into ~/.branvil/msb
  help         Print this message or the help of the given subcommand(s)

Options:
      --work-dir <WORK_DIR>
          Per-job work directories and the dependency cache [default: ci.executor.work_dir of the configuration, else ~/.branvil/runner]

      --url <URL>
          Base URL of the server [default: the server on this machine]

          [env: BRANVIL_URL]

      --token <TOKEN>
          The runner's token

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file for this machine [default: the one of the data directory]

      --data-dir <DATA_DIR>
          Data directory of the configuration [default: the server's]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics runner join

```text
Join this machine as a runner through the owner consent screen

Usage: guildbotics runner join

Options:
  -h, --help
          Print help
```

## guildbotics runner register

```text
Create a runner member and print its token once (needs the owner's token)

Usage: guildbotics runner register <NAME>

Arguments:
  <NAME>
          Name of the runner member, e.g. runner1

Options:
  -h, --help
          Print help
```

## guildbotics runner pull

```text
Pull an image into the runner's image cache (jobs never pull)

Usage: guildbotics runner pull <IMAGE>

Arguments:
  <IMAGE>
          Image reference by tag, [registry/]name[:tag], as pipelines name it

Options:
  -h, --help
          Print help
```

## guildbotics runner doctor

```text
Show which msb is used and whether the executor is ready

Usage: guildbotics runner doctor

Options:
  -h, --help
          Print help
```

## guildbotics runner install-msb

```text
Copy an msb binary (and the libkrunfw next to it) into ~/.branvil/msb

Usage: guildbotics runner install-msb --from <FROM>

Options:
      --from <FROM>
          Path of an msb binary that reports the pinned version

  -h, --help
          Print help
```
