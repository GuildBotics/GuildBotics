# Backup and restore

```sh
# On the Hub machine, while the service runs (the owner's token from the data directory):
branvil backup --to /srv/backups/branvil-2026-10-14

# Anywhere, the service need not run; the target must be a new directory:
branvil restore --from /srv/backups/branvil-2026-10-14 --data-dir /srv/branvil-restored
branvil admin check --data-dir /srv/branvil-restored
branvil serve --data-dir /srv/branvil-restored
```

`branvil backup` asks the running service (`POST /api/v1/admin/backup`) to
write the backup. The directory is on the machine the service runs on; a
relative `--to` is made absolute against the current directory of the
`branvil backup` command, so run it on the Hub machine. The service keeps
serving requests while the backup runs.

## What a backup contains

```
branvil-2026-10-14/          (0700)
├── manifest.json            versions, tables, repositories, checksums, exclusions (written last)
├── branvil.redb             the database as of one read transaction
├── branvil.toml             the configuration file
├── repos/<repo id>.bundle   `git bundle --all` of each repository with refs
├── ci/logs/, ci/artifacts/  CI logs and artifacts
└── attachments/             images attached to tickets
```

`manifest.json` (abridged):

```json
{"format": 1, "service": "branvil", "branvil_version": "0.1.0", "schema_version": 19,
 "git_version": "git version 2.54.0", "created_at": "2026-10-14T10:41:20.123Z",
 "base_url": "http://127.0.0.1:7878",
 "tables": {"tickets": 12, "pulls": 3, "reviews": 4, "...": 0},
 "repositories": [{"id": "repo_01K…", "path": "acme/demo", "default_branch": "main",
                   "bundle": "repos/repo_01K….bundle",
                   "refs": {"refs/heads/main": "<sha>", "refs/branvil/upstream/main": "<sha>"}}],
 "files": [{"path": "branvil.redb", "bytes": 565248, "sha256": "…"}, "…"],
 "excluded": ["secrets/ (…)", "secrets/accounts/ (…)", "secrets/webhook.url (…)", "hooks/ and git/ (…)", "Git reflogs (…)"],
 "account_tokens_excluded": [{"member": "aiko", "provider": "github"}]}
```

## How the backup stays consistent

Git refs and redb are separate stores, and a backup must not catch one
operation half in each. While the database is copied and the repositories are
bundled, the service holds the lock that serializes repository creation and
every repository's ref lock (`App::quiesce`, taken in id order). Every ref
change Branvil makes itself, and every recording of a push, runs under these
locks, so during the backup:

- no merge runs (a merge is journal entry → `git update-ref` → database update
  under the lock), so the database copy and the bundles never disagree about a
  merge; a journal entry caught in the copy is finished or dropped by the
  restored service exactly as after a crash;
- no push is recorded, the default branch does not follow its upstream, no
  repository is created.

The manifest is written last, to a temporary name that is synced and then
renamed, so a backup with a `manifest.json` is complete.

The database copy is one redb read transaction written into a new file
(`Store::export_to`), so it is one consistent state even though API writes
(tickets, comments, reviews) continue; anything written after that moment is
not in the backup.

Git itself can still accept a push while the locks are held (its
`post-receive` then waits for the lock). Such a push may be in a bundle but not
in the database: the same state as a push the service had not recorded when it
stopped. The restored service records it at startup (the startup rescan of
`docs/architecture.md`). Each bundle is self-consistent (`git bundle` resolves
the refs and packs exactly their objects), and the manifest lists the refs as
the bundle has them.

CI logs and artifacts are copied after the locks are released and after the
database copy. A file the copied database refers to is therefore in the backup
as long as CI writes a file before it records it, which it does (an artifact
is written, synced, renamed, and only then recorded; a log is a file only), and
nothing deletes it in between: from the database copy until the files are
copied, CI retention and the attachment sweep leave their deletions to their
next pass (`App::file_deletions`). A
file written later is an orphan the restored service removes at start. A log
copied while a job runs may hold a few more lines than the database snapshot
knows of. An attempt that was in progress at backup time is interrupted by
the restored service when its lease runs out (`docs/ci.md`).
Queued automatic retries retain their retry number. Eligible protected required
checks may queue another automatic retry after a second interruption, within
the three-retry budget. Restore does not restart completed failures on its own.

Tickets' attachments are copied the same way and are written before they are
recorded too, so every image the copied tickets and comments refer to is in
the backup, even if a later edit removed the last reference and the sweep
(`docs/storage-design.md`) then deleted it.

## Restore

`branvil restore --from <backup> --data-dir <new dir>`:

1. reads `manifest.json` and refuses another format or a schema version other
   than this build's or the one before it;
2. checks every file against its size and SHA-256 (a damaged backup is refused
   before anything is written) and refuses manifest paths that leave the
   backup directory;
3. refuses a data directory that exists and is not empty;
4. copies the database and the configuration, creates each repository as a
   managed bare repository and fetches its bundle (`+refs/*:refs/*`), and
   checks that the refs equal the manifest's;
5. copies CI logs and artifacts, and tickets' attachments;
6. upgrades the database when the backup has the previous schema version
   ("Upgrading"; it reads the restored repositories; the restored database is
   then marked, so it needs `branvil admin check` before it serves);
7. runs the checks of `branvil admin check` (redb structure, every index
   against the records) plus a Git directory for every repository record, and
   exits `1` when one fails.

The restored directory starts like any other with `branvil serve` (after a
passing `branvil admin check` when the restore upgraded it; its last line says
which); at startup it re-applies Branvil's Git configuration and hooks,
finishes or drops pending journal entries, and records refs the database had
not seen. Because a backup has no `secrets/owner.token`, the first start also
writes a new owner token into the restored data directory, revokes the
backed-up one, and starts with no GUI sessions; enter the GUI again with
`branvil open`.

Restoring over the original: stop the original service, restore into a new
directory, and start the restored one with the same `listen` and `base_url`
(they come from the restored `branvil.toml`), so every URL recorded in
GuildBotics stays valid. Two services on the same base URL cannot both listen.

## Credentials

| What | In the backup | Why |
| --- | --- | --- |
| Member tokens | Yes, as SHA-256 hashes in the database | The same tokens keep working after a restore, except the owner's token file's. A hash does not give the token, but protect the backup like the data directory (it is created `0700`). |
| The owner's token (`secrets/owner.token`) | No | The first start of the restored service writes a new one and revokes the old. |
| GUI sessions | Their records, but they do not survive | The new owner token's start deletes every session. |
| Members' upstream credentials (`secrets/accounts/`: tokens, GitHub App keys) | **No** | A credential for GitHub is a secret of another system; a backup copied to another disk or machine should not carry it. After a restore the accounts (provider, kind, and login) are there, but polling reports that the credential is not on this machine and nothing is read or sent until it is set again (`PUT …/members/{name}/accounts/{provider}`). `branvil backup` and `branvil restore` list these accounts. |
| The notification webhook's URL (`secrets/webhook.url`) | **No** | Like an upstream credential, it is a secret of another system. After a restore nothing is told until the owner sets it again (Settings → Notifications, `PUT /api/v1/admin/webhook`); it is then told of pending CI failures, automatic-retry recoveries, and passed-gate notices for pull requests still open on the host. |
| `secrets/cursor.key` | No | A new key is made at the first start; list cursors handed out before become `invalid_cursor`, which clients recover from by starting the list again. |
| The hook secret | No | It is made per process. |

## Not in a backup

- Git reflogs and repository-local Git configuration (Branvil re-applies its
  own configuration at every start).
- `hooks/` and `git/` (written at every start).
- Anything other than `ci/logs` and `ci/artifacts` under `ci/` (caches and job
  work areas are disposable).
- Workspaces, tickets, pull requests, reviews, and boards are in the database, so they are
  in a Branvil backup; the upstream host has only what was published there
  (`docs/security.md`).

## Upgrading

A build that changes the stored format raises the schema version
(`docs/storage-design.md`) and carries the upgrade from the version before
it, and no other. `branvil serve` and `branvil admin check|reindex` refuse a
database of the previous version with a message that names the upgrade:

```sh
branvil backup --to /srv/backups/branvil-before-upgrade   # with the old build, service running
# stop the service, install the new build
branvil admin upgrade --data-dir /srv/branvil
branvil serve --data-dir /srv/branvil
```

`branvil admin upgrade`:

1. refuses a database that another process has open, and any version other
   than the previous one (a current database is left as it is);
2. reads the records the upgrade changes, so that a record it does not
   expect stops it before anything is written;
3. writes a copy of the database as it is next to it,
   `branvil-schema-<version>-<time>.redb`, and never overwrites or removes
   one;
4. applies the upgrade and the new version in one write transaction, so a
   failure leaves the database at the previous version; the same transaction
   marks the database as upgraded and not yet checked (`meta["unchecked"]`);
5. prints what it changed, where the copy is, and that `branvil admin check`
   has to pass before `branvil serve` starts it.

While the database is marked, `branvil serve` refuses it; a passing
`branvil admin check` removes the mark. So whatever cut an upgrade off from
its check (a killed process, a reboot), the database is checked before it
serves. `branvil admin upgrade` exits 3 while the database is marked (it was
upgraded, now or before, and needs its check, even when nothing is left to
upgrade), 0 when it has this version and is not marked, and 1 when it failed,
which leaves it unchanged. `branvil restore` runs the same upgrade as one of
its steps (so a restored backup of the previous version is marked as well)
and exits 0 when the restore succeeds, upgraded or not.

To go back, stop the service, put the copy in place of `branvil.redb`, and run
the previous build. The copy holds the database only; the backup taken before
holds the repositories, logs, and artifacts as well. A backup of the previous
version is upgraded the same way by `branvil restore` (its copy lands in the
new data directory; when the upgrade fails, the restored directory is left
at the previous version, and `branvil admin upgrade --data-dir <it>` finishes
it once the cause is fixed).

Version 18 → 19 (`src/upgrade.rs`): CI attempts gain automatic-retry lineage,
runner-wait state, and the delivery time of their recovery notice. Existing
attempts keep their purpose, result, logs, and cache-refresh associations and
start without automatic-retry metadata or pending recovery notices. The
upgrade rebuilds indexes. It does not schedule retries of historical failures.

### The service run by scripts/serve.sh

The service that `scripts/serve.sh` runs from a checkout as a launchd user
agent (README, "Running the service from this checkout") is updated this way
with nobody watching. This section is the runbook for whoever handles its
troubles, typically an agent the owner instructs: it reads the log and the
files below, follows the operations, and checks the result by reading them
again. The operations change the production service, so ask the owner before
doing any of them.

`scripts/serve.sh` is the only process that starts or stops `branvil` and
runs updates. To start a build it runs that build's `branvil admin upgrade`
(nothing changes when the database already has its version), then
`branvil admin check` when the upgrade answered that the database is marked
(an upgrade changed it, now or at a start that was cut off, and no check has
passed since), then `branvil serve`, which is ready when it prints
`branvil listening on …`; it starts the service again when it exits. At its
start and every interval, while the service is ready, it runs
`scripts/update.sh`, which decides whether to update: when `origin/main` is
neither the running build nor `builds/attempted`, and that commit's run in
this Branvil has finished with every check the repository requires for a merge
(`required_checks`) succeeded (or it has no checks and expects none): what may
be merged may be installed, and a check that is not required decides nothing.
While checks are unfinished or failed, it reads them again in the next round;
a later successful retry allows the update. Required-check failures are told
by the service, for every repository's protected-branch run, rather than by
`update.sh`. After an hour from the first observation of a commit, the updater
tells each waiting reason once: unavailable API (connection failure, timeout,
or HTTP 5xx), no registered CI run, or queued/running checks. Authentication
failures, HTTP 4xx, invalid responses, and failures before a candidate is
fetched stop that round and queue a notice at once; their diagnostics stay in
the log. A pre-fetch failure never uses an installed commit's old waiting time. An update:

1. backs the running service up with the running build into
   `~/branvil-backups/branvil-update-<UTC time>` (keeping the newest three of
   these; only a backup with its `manifest.json` counts);
2. switches the checkout to `main` and fast-forwards it to `origin/main`;
3. installs the new build as `builds/<commit>` (from a checkout at exactly
   that commit with no uncommitted changes; the directory appears only once
   complete, and an existing one is kept);
4. rehearses: the running build restores that backup into
   `builds/rehearsal`, and the new build runs `admin upgrade` and
   `admin check` on that copy, which is then removed;
5. records the update in `builds/update`;
6. `scripts/serve.sh` names the new build in `builds/current`, stops the
   service, and goes on as the checkout's (possibly new) `scripts/serve.sh`.
   It stops the service with TERM, as whenever `builds/current` names another
   build: the service first gives out no new CI job and waits until the
   running ones finish, at most `ci.stop_wait_secs` (an hour by default;
   `docs/ci.md`, "Stopping the service"), still serving; the jobs queued
   meanwhile run after the new build starts. A job still running then is
   interrupted and queued again in its place, ahead of them, and the service
   exits with status 3, which is notified;
7. it starts the new build as above, and the update is done when that is
   ready. Failure is only the new build exiting: a start that takes long is
   waited for (after 30 minutes a notification says so, once). Then
   `builds/` keeps the new build and the one it replaced.

Steps 1 to 5, and the wait for the running CI jobs in step 6, run while the
old build serves, so the service is down only from the end of that wait until
the new build is ready. In that time, `admin check` works
through the database only when it is marked, that is when the update's
upgrade changed it (or an earlier one did and no check has passed since);
when the schema does not change, nothing in that time works through the data,
although `branvil serve`'s own start still grows with the number of
repositories.

**Files.** Each state file is written whole: to a temporary name, synced to
the disk, and renamed. Write them by hand the same way, for instance
`printf '%s\n' <commit> >builds/current.new && mv builds/current.new builds/current`.

| File | Meaning |
| --- | --- |
| `builds/current` | The commit of the build that runs (the checkout's `HEAD` can differ). Missing: Branvil is stopped on purpose. |
| `builds/<commit>/branvil` | An installed build. |
| `builds/update` | An update in flight: `<new> <previous> [<failed step>]`. |
| `builds/attempted` | The last commit whose installation, rehearsal, or activation failed after the backup; not tried again until `main` moves on. Failed checks never write it. |
| `builds/notified` | The last update/activation failure queued for notification. |
| `builds/checks-seen` | Commit and first-observed Unix time, retained across updater and service restarts. |
| `builds/told` | Commit/reason pairs already queued for a long-wait notice; successful waiting rounds do not clear it. |
| `builds/*.notice` | Pending script webhook notices, including the installed build that can send each one (updated before its old build is retired); first sent at the next delivery round (normally within `--interval` seconds; slow deliveries or service transitions can delay it), removed after successful delivery, and retried each interval even while the service is stopped. |
| `builds/serve.out` | The running serve's standard output (its `branvil listening on …` line, and `branvil stopping: …` while a stop waits for its running CI jobs or repository operations). |
| `builds/rehearsal`, `builds/*.new`, `builds/*.tmp`, `<data>/branvil-schema-*.redb.partial` | Left only by something cut off; removed at the next start. |
| `~/Library/Logs/branvil.log` | Everything `scripts/serve.sh` and `scripts/update.sh` print, the output of what they run (builds, Git, `branvil` commands), and the service's log (`branvil serve`'s standard error; its standard output is `builds/serve.out`). Lines from `scripts/serve.sh` and the service's log lines carry times. A failure is a line containing "stopped while" ("The update to … stopped while …" or "<commit> stopped while …"), "did not all succeed; it is not installed" (the commit's required checks; the service handles their webhook notice), or, for the first install, "Could not install the checkout's HEAD". A "stopped while" line with "(exit status 143)" is not a failure: the agent was stopped or restarted, and the next round runs that update again. A stop that waited for running CI jobs or repository operations logs "branvil serve of … stopped N seconds after TERM; it waited for its running work"; one that had to interrupt jobs logs and notifies "branvil serve of … stopped while CI jobs still ran". |

**The state.**

- Running: `builds/current` names a build, and `launchctl print gui/$(id -u)/com.guildbotics.branvil` shows the agent running. The build is ready once `builds/serve.out` has its `branvil listening on …` line (the log does not show it; the file is emptied only at the next start, so the line of a serve that has exited stays until then).
- An update in flight: `builds/update` exists. It goes on by itself, also after a restart; wait for it.
- The last failure: the last failure line of the log (see the table), and `builds/attempted`.
- Stopped: no `builds/current`. The log says why.

**What a failure leaves.** Checks are read again automatically. Once an update
has taken its backup and fails to install, rehearse, or activate that commit,
it waits for a person. Notices go to the configured webhook and the macOS
desktop. Webhook delivery failures remain queued, also across restarts.

| When | What runs then |
| --- | --- |
| Required checks fail or have not finished | The previous build; checks are read again each round and a later success allows installation. |
| A step up to the rehearsal fails (a build, the rehearsal's upgrade or check) | The previous build keeps running. A missing required command, the checkout check, reaching the running service, a failed fetch, or backup is tried again in the next round. |
| The new build's upgrade, check, or start fails | The previous build again, when its own `branvil admin upgrade` finds the database at its version (nothing changed it). Otherwise (the upgrade changed it) nothing: `builds/current` is removed, and the log names the database copy the upgrade kept. |
| A reboot, power loss, logout, or `launchctl bootout` at any moment | At its next start `scripts/serve.sh` goes on from these files: an update not yet recorded runs again in the next round, a recorded one is switched to and started, and one whose failure was recorded is concluded. A start cut off after its upgrade changed the database checks it at the next start. |

**Operations.** After each, read the files and the log again to see that it
took effect. Write files as above, never with a plain `>`.

- Start, restart, stop the agent. A restart also checks for an update at once
  and applies changes to `scripts/serve.sh`. A stop or restart stops the
  service with INT, also while it waits for its running CI jobs: it waits
  only for the repository operations already started, and the automatic
  runner's jobs are interrupted, and queued again in their place, at the
  service's next start. During an update it also
  waits for the update's running step; a step longer
  than the agent's `ExitTimeOut` (120 seconds, a build for instance) is killed
  then, and the next round runs that update again.

  ```sh
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.guildbotics.branvil.plist
  launchctl kickstart -k gui/$(id -u)/com.guildbotics.branvil
  launchctl bootout gui/$(id -u)/com.guildbotics.branvil
  ```

- Try a failed commit again, once its cause is fixed: remove
  `builds/attempted`, then restart the agent.
- To go back to the previous build, restore from a backup, or start a build
  that was stopped after its upgrade, first boot the agent out
  (`launchctl bootout …`) and wait until
  `launchctl print gui/$(id -u)/com.guildbotics.branvil` finds no such
  service: that stops the service and any update round, which could otherwise
  switch builds or start one on the data directory while you change it. Then
  `builds/update` must be absent; when it is there, an update was in flight:
  bootstrap the agent, let it finish (until `builds/update` is gone), and boot
  it out again. Then:
  - Go back (to a build kept in `builds/`): write `origin/main`'s commit into
    `builds/attempted` (otherwise the next round updates again); when an
    upgrade changed the database, put the copy named by the log's last
    `the database as it was: <data>/branvil-schema-….redb` line (one in the
    data directory, not in `builds/rehearsal`) in place of `branvil.redb`;
    write the previous commit into `builds/current`.
  - Restore: restore with a build that reads the backup's schema version (the
    manifest's `schema_version`; `branvil restore` takes that version or the
    one before it, which it upgrades and marks, so the start checks it):
    `builds/<commit>/branvil restore --from <backup> --data-dir <new dir>`;
    move the data directory aside (`mv <data> <data>.before-restore`) and the
    restored one into its place, or keep both and print the plist again with
    `--data-dir <new dir>`, from a shell whose `PATH` has `cargo`, `npm`, and
    `node` (use an absolute `<new dir>` in both commands: `branvil restore`
    resolves a relative one from the current directory, `scripts/serve.sh`
    from the checkout); when that build is older than `origin/main`, write
    `origin/main`'s commit into `builds/attempted`; write the build's commit
    into `builds/current`.
  - Start a build stopped after its upgrade, once the cause is fixed (when
    `admin check` reported index problems, run
    `builds/<commit>/branvil admin reindex --data-dir <data dir>`): write the
    commit into `builds/current`. The database is still marked, so the start
    runs `admin check` again before it serves.

  Then bootstrap the agent again; it starts the build `builds/current` names.

## Tested

`tests/backup.rs` creates a workspace and a repository with a push, a ticket on the workspace's board, a pull
request that closes it, a review, a conversation comment, an inline thread with
a reply, and an upstream (a GitHub stand-in) read with a member's account and
its token; it backs the service up while it runs, checks that the token is in
no file of the backup, that a damaged copy and one two schema versions old are
refused, turns the backup into one of the previous schema version, restores
that into another directory (upgrading and marking it: `branvil admin
upgrade` exits 3 until `branvil admin check` passes, then 0), runs
`branvil admin check`, starts the restored service on the
original address, and checks the refs (`git ls-remote`), the ticket and its
lane, the pull request and its ticket link, the review, the comments and
thread, the ticket's linked pull requests, the board cards, the repository (landing
nothing by itself, as the upgrade left it), the members and a member's
account (its login, while polling reports the credential missing), and that a new push is
recorded. `tests/ci.rs`
(`ci_records_logs_and_artifacts_survive_a_backup_and_restore`) backs up a
service with a finished CI attempt, restores it, and checks the checks, the
job log, and the artifact download.
