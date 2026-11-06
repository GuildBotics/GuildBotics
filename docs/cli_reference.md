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
- [`guildbotics environment remove`](#guildbotics-environment-remove)
- [`guildbotics environment login`](#guildbotics-environment-login)
- [`guildbotics environment usage`](#guildbotics-environment-usage)
- [`guildbotics environment image`](#guildbotics-environment-image)
- [`guildbotics environment image load`](#guildbotics-environment-image-load)
- [`guildbotics environment image load-ci`](#guildbotics-environment-image-load-ci)
- [`guildbotics environment image list`](#guildbotics-environment-image-list)
- [`guildbotics environment image declare`](#guildbotics-environment-image-declare)
- [`guildbotics server`](#guildbotics-server)
- [`guildbotics server start`](#guildbotics-server-start)
- [`guildbotics server stop`](#guildbotics-server-stop)
- [`guildbotics member`](#guildbotics-member)
- [`guildbotics member chat complete`](#guildbotics-member-chat-complete)
- [`guildbotics member chat identity`](#guildbotics-member-chat-identity)
- [`guildbotics member chat inspect channel`](#guildbotics-member-chat-inspect-channel)
- [`guildbotics member chat inspect thread`](#guildbotics-member-chat-inspect-thread)
- [`guildbotics member chat noop`](#guildbotics-member-chat-noop)
- [`guildbotics member chat post`](#guildbotics-member-chat-post)
- [`guildbotics member chat reaction add`](#guildbotics-member-chat-reaction-add)
- [`guildbotics member chat reply`](#guildbotics-member-chat-reply)
- [`guildbotics member chat resolve-channel`](#guildbotics-member-chat-resolve-channel)
- [`guildbotics member chat updates`](#guildbotics-member-chat-updates)
- [`guildbotics member checks await`](#guildbotics-member-checks-await)
- [`guildbotics member context`](#guildbotics-member-context)
- [`guildbotics member git commit`](#guildbotics-member-git-commit)
- [`guildbotics member git prepare`](#guildbotics-member-git-prepare)
- [`guildbotics member git publish`](#guildbotics-member-git-publish)
- [`guildbotics member git push`](#guildbotics-member-git-push)
- [`guildbotics member git show`](#guildbotics-member-git-show)
- [`guildbotics member help`](#guildbotics-member-help)
- [`guildbotics member memory archive`](#guildbotics-member-memory-archive)
- [`guildbotics member memory get`](#guildbotics-member-memory-get)
- [`guildbotics member memory promote`](#guildbotics-member-memory-promote)
- [`guildbotics member memory recall`](#guildbotics-member-memory-recall)
- [`guildbotics member memory record`](#guildbotics-member-memory-record)
- [`guildbotics member memory touch`](#guildbotics-member-memory-touch)
- [`guildbotics member memory update`](#guildbotics-member-memory-update)
- [`guildbotics member pr artifact download`](#guildbotics-member-pr-artifact-download)
- [`guildbotics member pr comment`](#guildbotics-member-pr-comment)
- [`guildbotics member pr create`](#guildbotics-member-pr-create)
- [`guildbotics member pr reaction add`](#guildbotics-member-pr-reaction-add)
- [`guildbotics member pr read`](#guildbotics-member-pr-read)
- [`guildbotics member pr reply`](#guildbotics-member-pr-reply)
- [`guildbotics member pr review`](#guildbotics-member-pr-review)
- [`guildbotics member pr review-comment`](#guildbotics-member-pr-review-comment)
- [`guildbotics member pr update`](#guildbotics-member-pr-update)
- [`guildbotics member repo-info read`](#guildbotics-member-repo-info-read)
- [`guildbotics member task complete`](#guildbotics-member-task-complete)
- [`guildbotics member task status`](#guildbotics-member-task-status)
- [`guildbotics member ticket attachment download`](#guildbotics-member-ticket-attachment-download)
- [`guildbotics member ticket comment`](#guildbotics-member-ticket-comment)
- [`guildbotics member ticket create`](#guildbotics-member-ticket-create)
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
- [`guildbotics branvil`](#guildbotics-branvil)
- [`guildbotics branvil init`](#guildbotics-branvil-init)
- [`guildbotics branvil serve`](#guildbotics-branvil-serve)
- [`guildbotics branvil open`](#guildbotics-branvil-open)
- [`guildbotics branvil member`](#guildbotics-branvil-member)
- [`guildbotics branvil member list`](#guildbotics-branvil-member-list)
- [`guildbotics branvil member create`](#guildbotics-branvil-member-create)
- [`guildbotics branvil member token`](#guildbotics-branvil-member-token)
- [`guildbotics branvil member account`](#guildbotics-branvil-member-account)
- [`guildbotics branvil repo`](#guildbotics-branvil-repo)
- [`guildbotics branvil repo create`](#guildbotics-branvil-repo-create)
- [`guildbotics branvil repo upstream`](#guildbotics-branvil-repo-upstream)
- [`guildbotics branvil workspace`](#guildbotics-branvil-workspace)
- [`guildbotics branvil workspace create`](#guildbotics-branvil-workspace-create)
- [`guildbotics branvil runner`](#guildbotics-branvil-runner)
- [`guildbotics branvil runner register`](#guildbotics-branvil-runner-register)
- [`guildbotics branvil runner pull`](#guildbotics-branvil-runner-pull)
- [`guildbotics branvil runner doctor`](#guildbotics-branvil-runner-doctor)
- [`guildbotics branvil runner install-msb`](#guildbotics-branvil-runner-install-msb)
- [`guildbotics branvil admin`](#guildbotics-branvil-admin)
- [`guildbotics branvil admin notify`](#guildbotics-branvil-admin-notify)
- [`guildbotics branvil admin check`](#guildbotics-branvil-admin-check)
- [`guildbotics branvil admin reindex`](#guildbotics-branvil-admin-reindex)
- [`guildbotics branvil admin upgrade`](#guildbotics-branvil-admin-upgrade)
- [`guildbotics branvil backup`](#guildbotics-branvil-backup)
- [`guildbotics branvil restore`](#guildbotics-branvil-restore)

## guildbotics

```text
GuildBotics

Usage: guildbotics <COMMAND>

Commands:
  environment     Manage this device's isolated environment
  server          Run the server
  member          Operate as a configured member through the running server
  git-credential  Supply Git with this machine's credential for the remote
  install         Install this executable and the GuildBotics skill for this user
  workspace       Select the active workspace for the server and AI CLI tools
  run             Run a command in the selected workspace's isolated environment
  diagnostics     Read a workspace's recorded diagnostics
  version         Print the binary's version
  secrets         Manage workspace or shared identity secrets
  service         Start or stop scheduled commands, ticket patrol, and chat events
  connect         Connect this machine's CLI through the server's consent screen
  open            Open the GuildBotics screens in the browser. The browser enters with a single-use link that expires after 60 seconds
  branvil         Branvil's commands (code hosting: `guildbotics branvil --help`)
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
  load     Load a Docker save or OCI archive built for this device's CPU
  load-ci  Load an archive into the workspace code host's CI runner
  list     List eligible local base images
  declare  Declare the image's digest per architecture in the selected workspace
  help     Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics environment image load

```text
Load a Docker save or OCI archive built for this device's CPU

Usage: guildbotics environment image load [OPTIONS] <ARCHIVE>

Arguments:
  <ARCHIVE>


Options:
      --tag <TAG>
          Add a reference to the archive's own tags

  -h, --help
          Print help
```

## guildbotics environment image load-ci

```text
Load an archive into the workspace code host's CI runner

Usage: guildbotics environment image load-ci <ARCHIVE>

Arguments:
  <ARCHIVE>


Options:
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
  start  Run the server in the foreground until SIGINT or SIGTERM (a first SIGINT waits for the commands running, SIGTERM or a second SIGINT cancels them): GuildBotics on GUILDBOTICS_SERVER_PORT (default 8765) with its records under GUILDBOTICS_HOME (default ~/.guildbotics), and Branvil, once `guildbotics branvil init` has set it up, as `guildbotics branvil serve` without options runs it (BRANVIL_DATA_DIR chooses its data directory)
  stop   Stop the server and wait for its work to end
  help   Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help
```

## guildbotics server start

```text
Run the server in the foreground until SIGINT or SIGTERM (a first SIGINT waits for the commands running, SIGTERM or a second SIGINT cancels them): GuildBotics on GUILDBOTICS_SERVER_PORT (default 8765) with its records under GUILDBOTICS_HOME (default ~/.guildbotics), and Branvil, once `guildbotics branvil init` has set it up, as `guildbotics branvil serve` without options runs it (BRANVIL_DATA_DIR chooses its data directory)

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

Start the GuildBotics server before running member commands. Use `member --workspace <path>` to verify the selected workspace.
See the [member capability reference](../guildbotics/templates/member/reference.md) for examples and option semantics.

### guildbotics member chat complete

```text
Usage: guildbotics member chat complete [OPTIONS]
  --person <VALUE> [required]
  --status <done|asking|blocked> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat identity

```text
Usage: guildbotics member chat identity [OPTIONS]
  --person <VALUE> [required]
  --format <json|markdown> [default: markdown]
  --help  Print help
```

### guildbotics member chat inspect channel

```text
Usage: guildbotics member chat inspect channel [OPTIONS]
  --person <VALUE> [required]
  --channel-id <VALUE> [default: ]
  --channel-name <VALUE> [default: ]
  --since <VALUE>
  --until <VALUE>
  --limit <VALUE> [default: 50; 1<=x<=200]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat inspect thread

```text
Usage: guildbotics member chat inspect thread [OPTIONS]
  --person <VALUE> [required]
  --channel-id <VALUE> [default: ]
  --channel-name <VALUE> [default: ]
  --thread-id <VALUE> [default: ]
  --message-url <VALUE> [default: ]
  --limit <VALUE> [default: 100; 1<=x<=200]
  --format <json|markdown> [default: json]
  --cursor <VALUE>
  --help  Print help
```

### guildbotics member chat noop

```text
Usage: guildbotics member chat noop [OPTIONS]
  --person <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat post

```text
Usage: guildbotics member chat post [OPTIONS]
  --person <VALUE> [required]
  --channel-id <VALUE> [default: ]
  --channel-name <VALUE> [default: ]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat reaction add

```text
Usage: guildbotics member chat reaction add [OPTIONS]
  --person <VALUE> [required]
  --channel-id <VALUE> [default: ]
  --channel-name <VALUE> [default: ]
  --message-id <VALUE> [default: ]
  --message-url <VALUE> [default: ]
  --reaction <ack|agree|celebrate|support> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat reply

```text
Usage: guildbotics member chat reply [OPTIONS]
  --person <VALUE> [required]
  --channel-id <VALUE> [default: ]
  --channel-name <VALUE> [default: ]
  --thread-id <VALUE> [default: ]
  --message-url <VALUE> [default: ]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat resolve-channel

```text
Usage: guildbotics member chat resolve-channel [OPTIONS]
  --person <VALUE> [required]
  --channel-name <VALUE> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member chat updates

```text
Usage: guildbotics member chat updates [OPTIONS]
  --person <VALUE> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member checks await

```text
Usage: guildbotics member checks await [OPTIONS]
  --person <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member context

```text
Usage: guildbotics member context [OPTIONS]
  --person <VALUE> [required]
  --check-credentials
  --format <json|markdown> [default: markdown]
  --help  Print help
```

### guildbotics member git commit

```text
Usage: guildbotics member git commit [OPTIONS]
  --person <VALUE> [required]
  --repo-path <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --workspace-mode <member|current> [default: member]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member git prepare

```text
Usage: guildbotics member git prepare [OPTIONS]
  --person <VALUE> [required]
  --issue-url <VALUE> [default: ]
  --pr-url <VALUE> [default: ]
  --repo <VALUE> [default: ]
  --branch <VALUE> [default: ]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member git publish

```text
Usage: guildbotics member git publish [OPTIONS]
  --person <VALUE> [required]
  --repo-path <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --workspace-mode <member|current> [default: member]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member git push

```text
Usage: guildbotics member git push [OPTIONS]
  --person <VALUE> [required]
  --repo-path <VALUE> [required]
  --workspace-mode <member|current> [default: member]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member git show

```text
Usage: guildbotics member git show [OPTIONS]
  --person <VALUE> [required]
  --repo <VALUE> [required]
  --path <VALUE> [required] (repeatable)
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member help

```text
Usage: guildbotics member help [OPTIONS]
  --help  Print help
```

### guildbotics member memory archive

```text
Usage: guildbotics member memory archive [OPTIONS]
  --person <VALUE> [required]
  --id <VALUE> [required]
  --team
  --policy-approved
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory get

```text
Usage: guildbotics member memory get [OPTIONS]
  --person <VALUE> [required]
  --id <VALUE> [required]
  --team
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory promote

```text
Usage: guildbotics member memory promote [OPTIONS]
  --person <VALUE> [required]
  --id <VALUE> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory recall

```text
Usage: guildbotics member memory recall [OPTIONS]
  --person <VALUE> [required]
  --query <VALUE> (repeatable)
  --meta-only
  --limit <VALUE> [default: 20; 1<=x<=200]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory record

```text
Usage: guildbotics member memory record [OPTIONS]
  --person <VALUE> [required]
  --scope <personal|team> [default: personal]
  --title <VALUE> [required]
  --summary <VALUE> [default: ]
  --keyword <VALUE> (repeatable)
  --ticket <VALUE> (repeatable)
  --pr <VALUE> (repeatable)
  --channel <VALUE> (repeatable)
  --thread <VALUE> (repeatable)
  --kind <note|policy> [default: note]
  --pin
  --content-file <VALUE>
  --content-stdin
  --policy-approved
  --set <VALUE> (repeatable)
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory touch

```text
Usage: guildbotics member memory touch [OPTIONS]
  --person <VALUE> [required]
  --id <VALUE> [required]
  --team
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member memory update

```text
Usage: guildbotics member memory update [OPTIONS]
  --person <VALUE> [required]
  --id <VALUE> [required]
  --team
  --title <VALUE>
  --summary <VALUE>
  --keyword <VALUE> (repeatable)
  --add-keyword <VALUE> (repeatable)
  --remove-keyword <VALUE> (repeatable)
  --ticket <VALUE> (repeatable)
  --pr <VALUE> (repeatable)
  --channel <VALUE> (repeatable)
  --thread <VALUE> (repeatable)
  --pin
  --unpin
  --kind <note|policy>
  --content-file <VALUE>
  --content-stdin
  --policy-approved
  --set <VALUE> (repeatable)
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr artifact download

```text
Usage: guildbotics member pr artifact download [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --name <VALUE> [required]
  --dest <VALUE> [default: .]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr comment

```text
Usage: guildbotics member pr comment [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr create

```text
Usage: guildbotics member pr create [OPTIONS]
  --person <VALUE> [required]
  --repo <VALUE> [required]
  --head <VALUE> [required]
  --base <VALUE> [default: ]
  --title <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --issue-url <VALUE> (repeatable)
  --closes-issue/--refs-issue [default: false]
  --draft <true|false> [default: false]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr reaction add

```text
Usage: guildbotics member pr reaction add [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --comment-id <VALUE> [required]
  --reaction <ack|agree|celebrate|support> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr read

```text
Usage: guildbotics member pr read [OPTIONS]
  --person <VALUE> [required]
  --resource <pull_requests|pull_request_comments|pull_request_reviews|pull_request_files|pull_request_threads|review_thread_comments|pull_request_readiness> [required]
  --subject <VALUE> [required]
  --params <VALUE> [default: ]
  --continuation <VALUE> [default: ]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr reply

```text
Usage: guildbotics member pr reply [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --reply-target-id <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr review

```text
Usage: guildbotics member pr review [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --event <approve|request_changes|comment> [required]
  --head <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr review-comment

```text
Usage: guildbotics member pr review-comment [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --head <VALUE> [required]
  --path <VALUE> [required]
  --line <VALUE> [x>=1] [required]
  --side <LEFT|RIGHT> [default: RIGHT]
  --start-line <VALUE> [x>=1]
  --start-side <LEFT|RIGHT>
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member pr update

```text
Usage: guildbotics member pr update [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --title <VALUE>
  --issue-url <VALUE> (repeatable)
  --closes-issue/--refs-issue [default: false]
  --drop-issue-links
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member repo-info read

```text
Usage: guildbotics member repo-info read [OPTIONS]
  --person <VALUE> [required]
  --resource <dependency_alerts> [required]
  --subject <VALUE> [required]
  --params <VALUE> [default: ]
  --continuation <VALUE> [default: ]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member task complete

```text
Usage: guildbotics member task complete [OPTIONS]
  --person <VALUE> [required]
  --waiting-for <VALUE> (repeatable)
  --status <done|asking|blocked> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member task status

```text
Usage: guildbotics member task status [OPTIONS]
  --person <VALUE> [default: ]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket attachment download

```text
Usage: guildbotics member ticket attachment download [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --id <VALUE> [required]
  --dest <VALUE> [default: .]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket comment

```text
Usage: guildbotics member ticket comment [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket create

```text
Usage: guildbotics member ticket create [OPTIONS]
  --person <VALUE> [required]
  --tracker <VALUE> [required]
  --title <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --label <VALUE> (repeatable)
  --add-to-board/--no-add-to-board [default: true]
  --interactive
  --assignee <VALUE>
  --lane <new|ready|in_progress>
  --parent <VALUE>
  --blocked-by <VALUE> (repeatable)
  --human-approved
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket reaction add

```text
Usage: guildbotics member ticket reaction add [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --comment-id <VALUE> [required]
  --reaction <ack|agree|celebrate|support> [required]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket read

```text
Usage: guildbotics member ticket read [OPTIONS]
  --person <VALUE> [required]
  --resource <tracker_tickets|tickets|ticket_comments|ticket_attachments|ticket_pull_requests> [required]
  --subject <VALUE> [required]
  --params <VALUE> [default: ]
  --continuation <VALUE> [default: ]
  --format <json|markdown> [default: json]
  --help  Print help
```

### guildbotics member ticket update

```text
Usage: guildbotics member ticket update [OPTIONS]
  --person <VALUE> [required]
  --url <VALUE> [required]
  --content-file <VALUE>
  --content-stdin
  --title <VALUE>
  --interactive/--no-interactive
  --add-label <VALUE> (repeatable)
  --remove-label <VALUE> (repeatable)
  --state <open|closed>
  --state-reason <completed|not_planned>
  --parent <VALUE>
  --add-blocked-by <VALUE> (repeatable)
  --remove-blocked-by <VALUE> (repeatable)
  --human-approved
  --format <json|markdown> [default: json]
  --help  Print help
```

## guildbotics git-credential

```text
Supply Git with this machine's credential for the remote

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
Run a command in the selected workspace's isolated environment

Usage: guildbotics run [OPTIONS] <COMMAND> [ARGS]...

Arguments:
  <COMMAND>
          Command name, optionally followed by @PERSON

  [ARGS]...
          Arguments for the command

Options:
      --person <PERSON>
          Member ID or name. Overrides a member specified as COMMAND@PERSON

      --cwd <CWD>
          Working directory for the command

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
Open the GuildBotics screens in the browser. The browser enters with a single-use link that expires after 60 seconds

Usage: guildbotics open [OPTIONS]

Options:
      --print
          Print the link instead of opening it, to paste into a browser on another machine

      --branvil
          Open the Branvil screens

  -h, --help
          Print help
```

## guildbotics branvil

```text
Branvil: local code hosting (Git over Smart HTTP, tickets, boards, pull requests, CI) for GuildBotics.

First run: `branvil init` creates the data directory, the configuration, and the owner, the one person who uses Branvil; the owner's token is kept in <data dir>/secrets/owner.token. Then `branvil serve` starts the service, and `branvil open` opens the GUI without a sign-in screen. The member, workspace, repo, backup, and open commands talk to the running service with the owner's token from that file (or --token / BRANVIL_TOKEN).

Usage: guildbotics branvil <COMMAND>

Commands:
  init       Create the data directory, the configuration, and the owner (its token goes to <data dir>/secrets/owner.token)
  serve      Run the service
  open       Open the GUI in the browser with a single-use link (on the Hub machine, as the owner)
  member     Manage agents, runners, and tokens (through the running service)
  repo       Manage repositories (through the running service)
  workspace  Manage workspaces (through the running service)
  runner     Run the CI runner, a separate process that executes jobs in microVMs (docs/ci.md)
  admin      Offline maintenance of the data directory (the service must be stopped)
  backup     Write a consistent backup of the running service into a new directory on its machine
  restore    Restore a backup into a new data directory and check it (the service need not run)
  help       Print this message or the help of the given subcommand(s)

Options:
  -h, --help
          Print help (see a summary with '-h')

  -V, --version
          Print version
```

## guildbotics branvil init

```text
Create the data directory, the configuration, and the owner (its token goes to <data dir>/secrets/owner.token)

Usage: guildbotics branvil init [OPTIONS]

Options:
      --owner <OWNER>
          Member name of the owner, the one person who uses Branvil

          [default: owner]

      --display-name <DISPLAY_NAME>
          Display name of the owner

      --listen <LISTEN>
          Loopback address written to the configuration

          [default: 127.0.0.1:7878]

      --base-url <BASE_URL>
          Permanent base URL written to the configuration [default: http://<listen>]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil serve

```text
Run the service

Usage: guildbotics branvil serve [OPTIONS]

Options:
      --listen <LISTEN>
          Loopback address to listen on (port 0 picks a free port) [default: listen of the configuration, else 127.0.0.1:7878]

      --base-url <BASE_URL>
          Permanent base URL [default: base_url of the configuration, else http://<bound address>]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil open

```text
Open the GUI in the browser with a single-use link (on the Hub machine, as the owner)

Usage: guildbotics branvil open [OPTIONS]

Options:
      --print
          Print the link (valid once, for 60 seconds) instead of opening a browser

      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil member

```text
Manage agents, runners, and tokens (through the running service)

Usage: guildbotics branvil member [OPTIONS] <COMMAND>

Commands:
  list     List members
  create   Create an agent or a runner
  token    Issue a token for a member (printed once)
  account  Set a member's account on an upstream host: a token, or a GitHub App's identity (owner)
  help     Print this message or the help of the given subcommand(s)

Options:
      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil member list

```text
List members

Usage: guildbotics branvil member list

Options:
  -h, --help
          Print help
```

## guildbotics branvil member create

```text
Create an agent or a runner

Usage: guildbotics branvil member create [OPTIONS] <NAME>

Arguments:
  <NAME>


Options:
      --display-name <DISPLAY_NAME>
          Shown name [default: the name]

      --kind <KIND>
          agent or runner

          [default: agent]

  -h, --help
          Print help
```

## guildbotics branvil member token

```text
Issue a token for a member (printed once)

Usage: guildbotics branvil member token [OPTIONS] <NAME>

Arguments:
  <NAME>


Options:
      --label <LABEL>
          [default: cli]

  -h, --help
          Print help
```

## guildbotics branvil member account

```text
Set a member's account on an upstream host: a token, or a GitHub App's identity (owner)

Usage: guildbotics branvil member account [OPTIONS] <NAME>

Arguments:
  <NAME>


Options:
      --provider <PROVIDER>
          [default: github]

      --token-env <TOKEN_ENV>
          Name of an environment variable holding the account's token (never pass it as an argument)

      --app-id <APP_ID>
          The app's id (with --installation-id and --private-key-file)

      --installation-id <INSTALLATION_ID>
          The id of the app's installation that reaches the repositories

      --private-key-file <PRIVATE_KEY_FILE>
          The app's private key (PEM file)

  -h, --help
          Print help
```

## guildbotics branvil repo

```text
Manage repositories (through the running service)

Usage: guildbotics branvil repo [OPTIONS] <COMMAND>

Commands:
  create    Create an empty managed repository in a workspace
  upstream  Make a repository follow an upstream repository and publish to it; an empty repository is filled with its default branch (owner)
  help      Print this message or the help of the given subcommand(s)

Options:
      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil repo create

```text
Create an empty managed repository in a workspace

Usage: guildbotics branvil repo create [OPTIONS] <PATH>

Arguments:
  <PATH>
          Repository path <workspace>/<name>, e.g. acme/demo

Options:
      --default-branch <DEFAULT_BRANCH>
          [default: main]

      --label <LABELS>
          A label its tickets may carry (repeatable)

  -h, --help
          Print help
```

## guildbotics branvil repo upstream

```text
Make a repository follow an upstream repository and publish to it; an empty repository is filled with its default branch (owner)

Usage: guildbotics branvil repo upstream [OPTIONS] --repository <REPOSITORY> <PATH>

Arguments:
  <PATH>
          Repository path <workspace>/<name>, e.g. acme/demo

Options:
      --repository <REPOSITORY>
          The upstream repository, e.g. acme/demo on GitHub

      --provider <PROVIDER>
          [default: github]

      --reader <READER>
          The member whose account reads it [default: kept, or picked for a new upstream]

  -h, --help
          Print help
```

## guildbotics branvil workspace

```text
Manage workspaces (through the running service)

Usage: guildbotics branvil workspace [OPTIONS] <COMMAND>

Commands:
  create  Create a workspace: its repositories live under it, and it has one board
  help    Print this message or the help of the given subcommand(s)

Options:
      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil workspace create

```text
Create a workspace: its repositories live under it, and it has one board

Usage: guildbotics branvil workspace create <NAME>

Arguments:
  <NAME>
          Workspace name, e.g. acme

Options:
  -h, --help
          Print help
```

## guildbotics branvil runner

```text
Run the CI runner, a separate process that executes jobs in microVMs (docs/ci.md)

Usage: guildbotics branvil runner [OPTIONS]
       guildbotics branvil runner <COMMAND>

Commands:
  register     Create a runner member and print its token once (needs the owner's token)
  pull         Pull an image into the runner's image cache (jobs never pull)
  doctor       Show which msb is used and whether the executor is ready
  install-msb  Copy an msb binary (and the libkrunfw next to it) into ~/.branvil/msb
  help         Print this message or the help of the given subcommand(s)

Options:
      --work-dir <WORK_DIR>
          Per-job work directories and the dependency cache [default: ci.executor.work_dir of the configuration, else ~/.branvil/runner]

      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil runner register

```text
Create a runner member and print its token once (needs the owner's token)

Usage: guildbotics branvil runner register <NAME>

Arguments:
  <NAME>
          Name of the runner member, e.g. runner1

Options:
  -h, --help
          Print help
```

## guildbotics branvil runner pull

```text
Pull an image into the runner's image cache (jobs never pull)

Usage: guildbotics branvil runner pull <IMAGE>

Arguments:
  <IMAGE>
          Image reference by tag, [registry/]name[:tag], as pipelines name it

Options:
  -h, --help
          Print help
```

## guildbotics branvil runner doctor

```text
Show which msb is used and whether the executor is ready

Usage: guildbotics branvil runner doctor

Options:
  -h, --help
          Print help
```

## guildbotics branvil runner install-msb

```text
Copy an msb binary (and the libkrunfw next to it) into ~/.branvil/msb

Usage: guildbotics branvil runner install-msb --from <FROM>

Options:
      --from <FROM>
          Path of an msb binary that reports the pinned version

  -h, --help
          Print help
```

## guildbotics branvil admin

```text
Offline maintenance of the data directory (the service must be stopped)

Usage: guildbotics branvil admin [OPTIONS] <COMMAND>

Commands:
  notify   Send text to the configured webhook without opening the database; works while the service is running or stopped; no-op if unset
  check    Check the database structure and that every index matches the records; a passing check also clears the mark an upgrade leaves, which `branvil serve` refuses
  reindex  Rebuild every derived index from the records
  upgrade  Upgrade the database from the previous schema version, keeping a copy of it as it was (stop the service and take a backup first). Exits 3 when the database was upgraded, now or before, and has not passed `branvil admin check` since; 0 when it already has this version and is checked; 1 when it failed, which leaves it unchanged
  help     Print this message or the help of the given subcommand(s)

Options:
      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil admin notify

```text
Send text to the configured webhook without opening the database; works while the service is running or stopped; no-op if unset

Usage: guildbotics branvil admin notify <TEXT>

Arguments:
  <TEXT>


Options:
  -h, --help
          Print help
```

## guildbotics branvil admin check

```text
Check the database structure and that every index matches the records; a passing check also clears the mark an upgrade leaves, which `branvil serve` refuses

Usage: guildbotics branvil admin check

Options:
  -h, --help
          Print help
```

## guildbotics branvil admin reindex

```text
Rebuild every derived index from the records

Usage: guildbotics branvil admin reindex

Options:
  -h, --help
          Print help
```

## guildbotics branvil admin upgrade

```text
Upgrade the database from the previous schema version, keeping a copy of it as it was (stop the service and take a backup first). Exits 3 when the database was upgraded, now or before, and has not passed `branvil admin check` since; 0 when it already has this version and is checked; 1 when it failed, which leaves it unchanged

Usage: guildbotics branvil admin upgrade

Options:
  -h, --help
          Print help
```

## guildbotics branvil backup

```text
Write a consistent backup of the running service into a new directory on its machine

Usage: guildbotics branvil backup [OPTIONS] --to <TO>

Options:
      --to <TO>
          Directory to create (must not exist or be empty; on the machine the service runs on)

      --url <URL>
          Base URL of the running service [default: base_url of the configuration, else http://127.0.0.1:7878]

          [env: BRANVIL_URL]

      --token <TOKEN>
          Token [default, except for `branvil runner`, `runner pull`, and `runner doctor`: the owner's token in <data dir>/secrets/owner.token]

          [env: BRANVIL_TOKEN]

      --config <CONFIG>
          Configuration file [default: <data dir>/branvil.toml]

      --data-dir <DATA_DIR>
          Data directory [default: the OS data directory for "branvil"]

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```

## guildbotics branvil restore

```text
Restore a backup into a new data directory and check it (the service need not run)

Usage: guildbotics branvil restore --from <FROM> --data-dir <DATA_DIR>

Options:
      --from <FROM>
          Backup directory written by `branvil backup`

      --data-dir <DATA_DIR>
          New data directory to fill (must not exist or be empty)

          [env: BRANVIL_DATA_DIR]

  -h, --help
          Print help
```
