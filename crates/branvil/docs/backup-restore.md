# Backup and restore

```sh
# On the Hub machine, while the server runs (its owner's token from the machine):
guildbotics server hub-data backup --to /srv/backups/hub-2026-10-14

# The server need not run; the target must be a new directory:
guildbotics server hub-data restore --from /srv/backups/hub-2026-10-14 --data-dir /srv/hub-restored
guildbotics server hub-data check --data-dir /srv/hub-restored
```

`guildbotics server hub-data backup` backs up the Hub's records; the code
host's part, described here, is its `code-host/` directory. The command asks
the running server (`POST /api/v1/admin/backup`) to write that part, as the
server's owner. The directory is on the machine the server runs on; a
relative `--to` is made absolute against the current directory of the
command, so run it on the Hub machine. The server keeps serving requests
while the backup runs. `restore` puts the code host's part into
`code-host/` of the new directory.

## What a backup contains

```
branvil-2026-10-14/          (0700)
├── manifest.json            versions, tables, repositories, checksums, exclusions (written last)
├── branvil.redb             the database as of one read transaction
├── config.toml              the configuration file
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
 "excluded": ["secrets/ (…)", "members' upstream credentials and the webhook URL (…)", "hooks/ and git/ (…)", "Git reflogs (…)"],
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

`guildbotics server hub-data restore --from <backup> --data-dir <new dir>`:

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
   then marked, so it needs `guildbotics server hub-data check` before it serves);
7. runs the checks of `guildbotics server hub-data check` (redb structure, every index
   against the records) plus a Git directory for every repository record, and
   exits `1` when one fails.

A server started on the restored data directory (`GUILDBOTICS_CODE_HOST_DATA_DIR`) uses it
like any other (after a passing `guildbotics server hub-data check` when the
restore upgraded it; its last line says which); at startup it re-applies the
service's Git configuration and hooks, finishes or drops pending journal
entries, and records refs the database had not seen. The owner is
GuildBotics' owner, as before.

Restoring over the original: stop the server, restore into a new directory,
and start the server on it at the same port, so every URL recorded in
GuildBotics stays valid.

## Credentials

| What | In the backup | Why |
| --- | --- | --- |
| Member tokens | Yes, as SHA-256 hashes in the database | The same tokens keep working after a restore. A hash does not give the token, but protect the backup like the data directory (it is created `0700`). |
| Members' upstream credentials (tokens, GitHub App keys) | **No** | A credential for GitHub is a secret of another system; a backup copied to another disk or machine should not carry it. It is in the service's vault ([security](security.md#upstream-tokens)), named after the data directory's path: a restore into the same path on the same machine, with the same vault, finds it again; a restore anywhere else has the accounts (provider, kind, and login), but polling reports that the credential is not on this machine and nothing is read or sent until it is set again (`PUT …/members/{name}/accounts/{provider}`). `guildbotics server hub-data backup` and `guildbotics server hub-data restore` list these accounts. |
| The notification webhook's URL | **No** | Like an upstream credential, it is a secret of another system, kept in the vault the same way. After a restore elsewhere nothing is told until the owner sets it again (Settings → Notifications, `PUT /api/v1/admin/webhook`); it is then told of pending CI failures, automatic-retry recoveries, and passed-gate notices for pull requests still open on the host. |
| The cursor signing key | No | It is made per process: list cursors handed out before a start become `invalid_cursor`, which clients recover from by starting the list again. |
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

Successive verified commits may raise the schema version by at most one, so
each verified build remains reachable with a single upgrade. While a schema
raise is still unverified, add further format changes to that same step's
upgrade. The `schema-raise` CI job checks this against `GB_CI_VERIFIED_SHA`
on default-branch verification and rejects changes to `upgrade.rs` at an
unchanged verified version. Without a verified commit it skips the check.

A build that changes the stored format raises the schema version
(`docs/storage-design.md`) and carries the upgrade from the version before
it, and no other. The server and `guildbotics server hub-data check|reindex`
refuse a database of the previous version with a message that names the
upgrade:

```sh
guildbotics server hub-data backup --to /srv/backups/hub-before-upgrade   # with the old build, server running
# stop the server, install the new build
guildbotics server hub-data upgrade   # converts, then checks
guildbotics server start
```

`guildbotics server hub-data upgrade`:

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
5. prints what it changed, where the copy is, and that `guildbotics server hub-data check`
   has to pass before the server opens it; `upgrade` then runs that check.

While the database is marked, the server refuses it; a passing
`guildbotics server hub-data check` removes the mark. So whatever cut an upgrade off from
its check (a killed process, a reboot), the database is checked before it
serves. `guildbotics server hub-data restore` runs the same upgrade as one of
its steps (so a restored backup of the previous version is marked as well).

To go back, stop the service, put the copy in place of `branvil.redb`, and run
the previous build. The copy holds the database only; the backup taken before
holds the repositories, logs, and artifacts as well. A backup of the previous
version is upgraded the same way by `guildbotics server hub-data restore` (its copy lands in the
new data directory; when the upgrade fails, the restored directory is left
at the previous version, and `guildbotics server hub-data upgrade --data-dir <it>` finishes
it once the cause is fixed).

Version 46 → 47 (`src/upgrade.rs`): the configuration file `branvil.toml` in
the data directory is copied to `config.toml`, and the automatic runner's
stored display name changes to "GuildBotics runner (automatic)" unless the
owner renamed it. `branvil.toml` stays for the previous build, which a rolled
back update runs again; the owner removes it once the update is confirmed. A
backup of version 46 holds the file under its old name; restore places it as
it is, and its upgrade copies it. Every upgrade also binds the GuildBotics
workspaces' execution roots.

### The integrated Hub service

On macOS the Hub owner runs [`scripts/hub-server.sh update`](../../../docs/hub-server.md#手動更新).
The running build saves a backup of the Hub's records, the script keeps three
completed generations, and the new build restores a copy to upgrade and check. Only a passing
rehearsal allows the script to stop the running service and upgrade the live
data. The Hub runbook describes how the previous build is restarted and how
to recover when that build needs the old schema.

## Tested

`tests/backup.rs` creates a workspace and a repository with a push, a ticket on the workspace's board, a pull
request that closes it, a review, a conversation comment, an inline thread with
a reply, and an upstream (a GitHub stand-in) read with a member's account and
its token; it backs the service up while it runs, checks that the token is in
no file of the backup, that a damaged copy and one two schema versions old are
refused, turns the backup into one of the previous schema version, restores
that into another directory (upgrading and marking it until its check
passes), runs the check, starts the restored service on the original
address, and checks the refs (`git ls-remote`), the ticket and its
lane, the pull request and its ticket link, the review, the comments and
thread, the ticket's linked pull requests, the board cards, the repository (landing
nothing by itself, as the upgrade left it), the members and a member's
account (its login, while polling reports the credential missing), and that a new push is
recorded. `tests/ci.rs`
(`ci_records_logs_and_artifacts_survive_a_backup_and_restore`) backs up a
service with a finished CI attempt, restores it, and checks the checks, the
job log, and the artifact download.
