# CI

Branvil runs a small CI of its own: a pipeline file in the repository, a
persistent queue in the service, and a runner process that executes each job
in a throwaway microVM. Its results are the checks of `GET
/api/v1/repos/{repo}/pulls/{n}/checks` and landing conditions (required checks accept service path skips).

```
git push ─▶ guildbotics server start ─ register run (pipeline from the commit) ─────────▶ redb queue
                 ▲                                                               │ claim (lease)
                 │  heartbeat · log · artifacts · finish                         ▼
                 └──────────────────────────────────────────────── guildbotics runner ─ msb run ─▶ microVM
```

## Images on runners of different CPUs

In the background, a runner reads the Hub's archive catalog for its own CPU
(`arm64` or `amd64`), downloads missing or changed versions, and loads them under
its image-cache lock. Equal configuration digests cause no download or load.
Claims advertise locally held tags, CPU architectures, and configuration digests.
The Hub hands out only compatible work. A missing image on every live runner
ends the queued attempt as `startup_failure` after `runner_absent_secs`, with the
missing tag and CPU in its details. Automatic retries wait for a compatible runner.

**Settings → Agent execution environment → Images** and
`guildbotics environment image load --ci <archive>` accept archives for either CPU. The Hub retains the archives under its data directory; native images also enter the Hub's shared image store. The Hub's CI and isolated commands both use this store. `scripts/build-agent-environment-image.sh` sends
every built architecture to the Hub. Registry images are pulled on each runner
with `guildbotics runner pull <tag>`; jobs themselves never pull.

## Default-branch verification

The default branch is verified serially per repository, separately from ordinary
ref-following runs. When no verification is running and the tip differs from the
last verification SHA, Branvil records `running` before registering all jobs at
that tip with protected cache scope. Queue capacity never blocks these jobs.
Main advancing leaves those queued and running attempts intact. Completion starts
one verification at the then-current tip, batching intervening merges. The minute
reconciler restores registration from the durable running record after a crash.
Check events and ref-change batches advance verification under the repository lock.

All checks must complete, including invalid-pipeline markers. All must succeed;
a commit without a pipeline also passes. Required checks govern publication and
merging on PR heads. Main verification runs *all* of the commit's own jobs, so
every job its pipeline marks `required` exists there and must succeed.
Only the default branch is verified: merging into another base branch does not
start verification of that base.

Before recording success, Branvil creates an immutable internal
`refs/branvil/verified/<sha>` ref. These refs are hidden from Git clients and cannot
be pushed, even by the owner. The record retains the last verified SHA and time,
the verification's starting base, failed jobs, and the first failing SHA since the
last success. A crash between ref creation and the success transaction is safe to
resume. Completion detaches main from the old run if the branch has advanced.

A member or owner retry of an unsuccessful job of a failed verification reopens
it atomically only while main still points to that SHA. This retry bypasses queue
capacity, preserves `failing_since`, and clears the failed-job list. Other manual
retries change neither the verification record nor its verified ref.

Verification attempts use class **4m**, after merge-directed work
(classes 1–3 and 4a), before drafts (4b). They display
`Deferred: main is verified after the jobs that lead to a merge.` They do not
consume admission capacity for any class. Manual and automatic retries of a
failed job of the current verification use class **1** and count in ordinary
class 1–3 admission. Other jobs keep class 4m across failed attempts and subsequent
verifications. Other protected branches retain class 1.

Jobs declaring `verified_sha = true` record the latest verified SHA when claimed,
return it in the claim response, and receive `GB_CI_VERIFIED_SHA` (empty before
any success). Reuse for these jobs requires both the same verified SHA and the
same commit tree, even when `paths` match: ancestry of a verified commit is not a
property of those paths. Undeclared jobs retain the usual reuse rules and have
no such environment variable. Protected registration reruns unsuccessful attempts
previously executed for PRs; successful cacheless actual executions remain valid.

## The pipeline: `.guildbotics-ci.toml`

```toml
version = 1                       # optional; only 1 exists

[[jobs]]
name = "test"                     # [A-Za-z0-9_.-]{1,64}, not "pipeline", unique
image = "docker.io/library/alpine:3.22"   # [registry/]name[:tag], no digest
commands = ["./scripts/test.sh", "make lint"]   # 1-64 commands, each run by /bin/sh -c
paths = ["src/**", "tests/*.rs", "Cargo.lock"]  # optional; empty/omitted runs every change
verified_sha = false             # optional; declares GB_CI_VERIFIED_SHA as an input
required = true                   # optional; PRs into this branch must pass it
post_merge = false                # optional, not with required; PR heads skip the job
timeout_secs = 600                # optional, 1-86400; default the runner's time limit
tmpfs_mib = 2048                   # optional, 1-4096; /tmp uses a 4 GiB disk by default
# ram_workspace = { paths = ["out/cache"], max_mib = 4608 }  # optional, see below
artifacts = [{ name = "report", paths = ["out/report/", "coverage.xml"] }]   # up to 16
cache = { paths = ["target"], key_files = ["Cargo.lock"] }                    # optional
```

An optional `[release]` section gives the release rule ([Releases](#releases)).

- Commands run in order in `/work`; the first one that fails ends the job
  with its exit code. The job's environment has only CI metadata (besides
  the image's own variables and `HOME`): `CI=true`,
  `GB_CI_REPOSITORY`, `GB_CI_SHA`, `GB_CI_JOB`, `GB_CI_ATTEMPT`,
  `GB_CI_ATTEMPT_ID`. Jobs declaring `verified_sha = true` also receive
  `GB_CI_VERIFIED_SHA` (the latest verified commit at claim, or empty). No tokens, no host variables. See "The job's
  environment" for `/work`, the user, and `/tmp`.
- Jobs of one run are queued together; a runner runs queued jobs, of any
  run, up to the runner's configured `max_jobs` at once, each in a microVM of its own
  (there are no dependencies between jobs).
  Admission assigns sequence numbers within a run in this order:
  failures in the previous run, jobs without a result, then successes (including
  reused successes). The previous run is the nearest recorded run on the first
  20 first-parent ancestors, excluding the commit itself. Failures include failure,
  timeout, startup failure, and cancellation other than supersession. Failures
  of a protected-branch run are treated as jobs without a result; so are missing,
  unfinished, superseded, path-skipped, and newly added jobs.
  Within each group, jobs with a known estimate go first, shortest first;
  equal estimates and jobs without estimates keep pipeline order. Estimates use
  the nearest non-reused successful duration in the same history as the running
  checks' estimates. If Git cannot read history, pipeline order is used.
  Registering the same run again does not change existing attempts or their
  sequence numbers. Waiting runs receive this order when admitted; individual
  retries and interrupted attempts keep their existing rules.
  At each claim the service derives a class from current store records, without
  Git or a saved priority. The first matching rule wins:

  | Scope | Condition | Class |
  | --- | --- | --- |
  | Attempt | Probe (even with a protected run, or without a run) | 2 |
  | Run | Missing run or repository, or no branches want the commit | 4b |
  | Branch | Protected, except ordinary default-branch verification (4m) | 1 |
  | Branch | No open PR using this head branch | 4b |
  | PR | Draft | 4b |
  | PR | A required check completed without satisfying the head, including a missing required job | 4a (deferred) |
  | PR | A current changes-requested verdict, too few approvals, and an unanswered statement for the author | 4a |
  | PR | Linked to the open repair ticket | 2 (before other class 2 work) |
  | PR | Enough current approvals and not held by `landing::held` | 2 |
  | PR | Otherwise | 3 (normal) |

  While verification has `failing_since`, required failures with names in its
  `failed_jobs` or the open repair ticket's `repair.jobs` do not demote a head to
  4a. This also applies while verification is running with empty `failed_jobs`.
  Once verification passes, all required failures count again.

  A run takes the highest class of its branches; a branch takes the highest
  class of its open PRs. Review verdicts, approvals, unanswered statements, and
  required checks use the same rules as PR readiness and landing. Reasons for
  4a/4b distinguish missing records, unwanted commits, no open
  PR, drafts, the failed required check, and the reviewer requesting changes
  (with the approval count).

  Queued check and job `details` show the deferred reason and when the job can
  run for classes 4a/4b, after any automatic retry explanation. Waiting runs
  append the deferred reason to their queue-capacity message.

  Claim order is 1, 2, 3, 4a, 4m, 4b. Within a class, a runner first takes
  attempts whose latest reusable successful result (of the same tree, or
  of the same contents of paths; "Trust") matches its job image digest.
  Those claims reuse the result immediately without reserving VM resources.
  An attempt whose candidate has a different image digest joins the ordinary
  execution tier.
  Registration sequence (`seq`) orders attempts within each tier.
  Reuse is assessed after taking the run's current cache scope; retries with
  `allow_reuse = false` execute. Among ordinary checks of the same repository
  and commit in the same class, a runner prefers jobs it can start with a
  [RAM workspace](#ram-workspace).
  It chooses the job that the fewest other online, accepting runners can run
  in RAM, with queue order breaking ties. This count uses their reported
  machine capacity, VM limits, cache inventory, and image support, including
  runners with full slots or low available memory. A runner can still take
  its first job to keep both slots working if another runner would take the
  RAM choice and cannot start that first job. A runner with only disk choices
  can take another job in the group to leave its first job for a peer that can
  start it in RAM.
  Class 4a runs only when no class 1–3 attempt waits, and 4b only when no other
  class waits. Priority changes alone neither cancel nor hold attempts.
  When the author starts addressing PR feedback, running checks of a deferred/last
  head may be yielded: the running attempt is asked to cancel and a new current
  attempt is queued at the same position. Queued checks are untouched. This is
  author-only and does not apply if a protected branch or a higher-priority PR
  also wants the commit. The old cancellation is not the current check result.
  A free runner may immediately claim the replacement; a push stops obsolete work.
  Otherwise, a deferred/last attempt that has started continues to completion, even if
  higher-class work arrives. Under sustained load a failing head's remaining
  jobs may therefore finish only after its author has already pushed a fix.

- `paths` declares the files that decide the job: its result and artifacts
  depend only on the files matching `paths` (on the whole tree when empty).
  A job that reads other files must add them to `paths`, or leave `paths`
  empty. Patterns use Git's pathspec glob rules (up to 32
  relative paths of at most 512 bytes; no `:`, `\\`, NUL, empty, `.` or `..`
  components). `**` matches recursively; `*` does not cross directories.
  On a PR head, a job whose matching files did not change since the
  merge-base is skipped (Git's `diff-tree -r --quiet --no-renames` from
  merge-base to head, including deletions and both sides of renames), and a
  job whose matching files did change may reuse a success with the same input
  digest of those files ("Trust"). Protected branches reuse only
  the same tree and never take a head's reuse by paths as their result, so a
  file missing from `paths` shows there. Empty `paths` runs every time.
  Changing `paths` changes the job definition used for reuse.
- Validation is strict: an unknown key (`cpus` and `memory_mib` included,
  with a reason of their own: they are the runner's), a value out of range, a
  path that is absolute or contains `..`, an image reference that is not
  `[registry/]name[:tag]` (one with a digest, `@sha256:…`, included) makes the
  whole pipeline invalid. An invalid pipeline is shown as a failing check
  named `pipeline` whose details give the reason; it never yields success.
- What a job does (commands, artifacts, cache, how long it may take) is the
  repository's; what it runs on (the image's version, the CPUs and memory) is
  the owner's. A job runs with the runner's CPUs and memory
  (`ci.executor.max_cpus`, `max_memory_mib`), and with its `timeout_secs`
  lowered to the runner's time limit (`max_timeout_secs`, noted in the log),
  or with that limit when it gives none.
- A pipeline names an image by its tag; which version runs is the image the
  runner's cache holds under that tag. The runner never pulls during a job
  (pull policy `never`): images enter the runner's cache through the Hub archive sync,
  `guildbotics runner pull` or **Settings → Agent execution environment → Images**
  (pull a tag from a registry or load a `docker save` / OCI archive from a file on the Hub). To move
  the jobs to a new version, pull or load the same tag again; nothing in the
  pipelines changes. A job whose tag the cache does not hold concludes
  `startup_failure` with the reason.
- A job's log starts with the attempt: its job, commit, executor, image tag,
  CPUs, memory, and time limit (and a note when its `timeout_secs` was
  lowered). Once the runner has checked the image, it writes the
  configuration digest of the image the job runs on in a line of its own
  (`image docker.io/library/alpine:3.22 is sha256:…`) and records it on the
  attempt: the configuration digest survives `docker save` and loading, the
  manifest digest does not. An image the cache does not hold, or whose
  digest `msb` does not report as `sha256:<64 hex>`, ends the job
  `startup_failure` with the reason. Pulls and
  loads hold the image cache lock (a file of `MSB_HOME`) exclusively, whether
  started from the image settings or with `guildbotics runner pull`. A job holds
  it shared from checking its image until its VM has started, so a load that re-points the tag waits
  for the jobs that are starting and never changes the image a checked job
  runs (with several jobs at once, a pull or a load waits for a moment when
  no job is starting); a job that waits for a pull or a load says so in its
  log and can be cancelled meanwhile (`msb` used by hand does not take the
  lock). **Settings → Agent execution environment → Images** shows the tag
  to write and the digest of every cached image. (A runner started by hand on another machine loads such
  an archive with `msb load` under its own `MSB_HOME`.)
- The Hub loads each archive image with a manifest digest reference. Only the
  snapshot-aware sweep removes unused versions from the shared store. Tags stay
  available for future jobs and isolated commands.
- An external runner also names the version a pull or load now points to by its
  manifest digest (`<name>@sha256:…`), as msb cannot remove a version that no
  reference names. After a pull or a load, and before each job, the runner
  removes the versions no tag points to anymore, with their layers; msb
  refuses one a job still runs on, which goes at a later job's start. These
  digest references are not tags a pipeline can name. The image settings
  list only images named by tag. A load pins the reference it changed in the cache, the name
  msb registered (for a `docker save` archive its `RepoTags`, not the
  `docker.io/…` name of its `index.json`). Loading a version the cache already
  holds pins it if it has no pin yet (loaded by hand, before pins existed, or
  after a failed pin); one with its pin is not tagged again. An archive of
  several images is not pinned (msb names only its first image when it
  pins, and the references the load changed do not tell which of them is
  the first image's), and an image loaded with `msb` by hand is not pinned
  until it is loaded again here: its version stays in the cache when the tag
  moves.

## The job's environment

What a job's commands find, as on common CI services:

- **`/work` is a Git checkout of the commit.** It has `.git` and an index,
  `HEAD` is the commit (detached), and it holds the commit's history and the
  tags whose commits it reaches, so `git ls-files`, `git describe --tags`,
  and version tools that read Git (`hatch-vcs`, `setuptools-scm`) work. It
  has no remote, no credentials, and no Git configuration besides the
  repository's own (no reflog of the checkout either). Each tracked file has
  the mode and the kind the index records (an executable and a symbolic link
  stay so even when the runner's machine has neither), and Git's settings
  are those of `/work` (`core.filemode` and `core.symlinks` on,
  `core.ignorecase` and `core.precomposeunicode` off), not of the runner's
  machine. The files have the time of the checkout, not the commit's, and the
  index compares only modification times, sizes, and modes
  (`core.checkStat=minimal`, `core.trustCtime=false`): the VM sees other
  owners, inodes, and change times than the runner's machine wrote, and
  `git diff-index --quiet HEAD` must not report the checkout modified. Git
  attributes and ignore rules of the runner's machine do not apply. Being a
  checkout and not an archive, `export-ignore` and `export-subst` attributes
  do not apply either.
- **The commands run as a fixed user that is not root**, uid 1000 and gid
  1000 (`msb run --user 1000:1000`), without capabilities: a file of mode 0
  cannot be read. `/work` and the cache paths restored into it are that
  user's, and the log file is presented to the VM as that user's. `HOME` is
  the home the image's passwd gives uid 1000 when that user can write it, and
  `/tmp/home` (created) otherwise; the user may not exist in the image's
  passwd.
- **`/tmp` is private to the job**, writable and executable by the job user
  (mode 1777). By default it is a 4 GiB ext4 disk, a named volume
  `bv-tmp-<runner>-<attempt id>` that the runner removes after the job. A job
  can instead set `tmpfs_mib` to an integer from 1 to 4096. The runner then
  mounts a tmpfs of that maximum size and creates no `/tmp` volume. Files
  written there use the job VM's memory, so the job's commands and temporary
  files share its memory limit. A change to `tmpfs_mib` changes the job
  definition used to match reusable results. The VM's root disk still uses
  microsandbox's 4 GiB default. `/work` is an ext4 disk of the job's own, a named volume
  `bv-work-<runner>-<attempt id>`: what the job may add to it beyond the
  checkout and the restored cache is `ci.executor.work_quota_mib` (16 GiB by
  default; a workspace built from scratch outgrows microsandbox's 4 GiB
  default). They are the runner's, not the pipeline's.

### RAM workspace

Set `ram_workspace = { paths = ["out/cache"], max_mib = 4608 }` on a job to
keep changes to the selected directories in RAM. Specify one to eight distinct,
non-overlapping paths relative to `/work`. Each path must name a directory
that exists after checkout and cache restore, without symbolic links. The
directories keep their usual paths. The RAM changes disappear when the VM shuts down, after any
required cache and artifact data have been collected.
The selected directories share one tmpfs of 1–65536 MiB. The job's separate
`tmpfs_mib` continues to control `/tmp`.

The Hub SDK and remote `msb` CLI use the same Linux guest implementation.
The image needs Python 3.8 or later and the `mount`, `umount`, `setpriv`,
and `unshare` tools. The guest kernel must support OverlayFS, tmpfs, and a PID
namespace for the job processes. RAM can be used with an exact hit or a
restored compatible older cache. Jobs with no restored cache use disk, which
allows clean builds to remove and recreate the entire work directories.

For RAM jobs that need to save a new cache, the guest image also needs
`mkfs.ext4` (from e2fsprogs) to create a temporary ext4 image during preparation.
The preparation VM uses that image to verify that `mount -t ext4 -o loop` and
`umount` work with the guest kernel. `mount` sets up the loop device itself.
If the ext4 probe fails, including when `mkfs.ext4` is missing, the runner
logs an ext4 loop-mount probe failure and runs the job on disk. Jobs known in
advance to need no new cache skip the probe.

When saving a new cache from RAM, the runner creates the cache volume only
after the commands succeed. The job VM loop-mounts the preformatted ext4
image and copies the declared paths from the merged workspace to that volume
while it still holds the RAM changes. Files pass through memory on their way
to the volume.
Collection can finish after the command deadline. The published cache is
reusable on subsequent jobs. With an exact hit the runner skips saving a new
cache. The runner keeps 1024 MiB of VM memory beyond the sum of both tmpfs
limits for commands. It also checks
that the host's usable physical memory can cover a full VM for each configured
slot and that current available memory can cover this VM, retaining the runner's
configured reserve in both checks. On Windows it also checks available commit.
For example, start with an 8192 MiB VM, `max_mib = 4608`, and
`tmpfs_mib = 2048`. The job log records whether RAM was enabled and the reason
when it uses disk.

The runner packs changed files at declared artifact paths while the job VM
still holds the RAM changes. Unchanged files from the cache and deleted files
are omitted. Successful and failed commands retain their exit codes, and
timed-out jobs can return completed artifacts. Cancelled jobs return no
artifacts. Artifact limits and download routes are the same as for disk jobs.

Deploy the version that supports `ram_workspace` to the Hub and every runner
that can claim the job before committing a pipeline that uses it. Verify that
the Runners view reports the same updated build version as the Hub. An older
Hub rejects the new pipeline key. The optional real-VM fixture uses a disposable
runtime data directory with `guildbotics/agent-environment:dev` already loaded.

```sh
GUILDBOTICS_REAL_EXECUTOR=1 GUILDBOTICS_REAL_EXECUTOR_DATA=/absolute/test-data \
  cargo nextest run --locked -p gb-runner --features host --run-ignored only -E 'test(ram_workspace_in_real_microvms)'
```

Set `GUILDBOTICS_RAM_TEST_EXECUTOR=cli` to exercise the remote CLI path against
the same fixture. To cover customized guests, set
`GUILDBOTICS_RAM_TEST_IMAGE_NO_LOSETUP` to a loaded image with `losetup` removed
and `GUILDBOTICS_RAM_TEST_IMAGE_NO_LOOP` to a loaded image where `mount -o loop`
fails. Both images must still provide the other preparation tools. The fixture
checks that the first image saves a cache from RAM and that the second saves
it through the disk collector.

## Trust: where the pipeline comes from

Every commit runs its own pipeline: a tip of the default branch and the head
of an open pull request alike read `.guildbotics-ci.toml` from that commit. A pull
request that changes the pipeline runs the changed pipeline as its checks, so
a mistake in it shows before the merge. Main verification checks the combination after merge.
PR-head jobs with `paths` run if any matching file changed since the
merge-base with the current base tip of any open PR sharing that head SHA
(and may then reuse a success with the same contents of paths, below).
A changed `.guildbotics-ci.toml`, a missing base, or no merge-base runs all jobs.
The run saves each PR's base name, tip and merge-base and its skipped job
names. Re-registration updates this snapshot when PRs or base tips change;
unchanged inputs do not create new attempts. Queue admission after waiting
uses the saved selection and counts only jobs that actually run. Protected
branches always run every job when registered by the triggers below;
protection alone does not register a non-default branch. A protected
registration of a SHA that had path skips queues those jobs too, and so are
the jobs whose current attempt reused another tree's result by the contents
of paths. Such a reuse reported after the commit became a protected branch's
ends `cancelled` ("Stopped: the commit became a protected branch's; it runs
again as one.") and a new attempt takes its place in the queue.

Service path skips are completed `skipped` attempts with `path_skip`
metadata and a `[guildbotics] skipped: no file matching paths changed since …`
log line naming the PR/base/merge-base snapshot. They satisfy required head
checks, never supply reusable results or duration estimates, and a manual
retry executes the job. On reselection, a newly path-excluded queued attempt
becomes a completed service skip as well; running executions and manual retries
are preserved. A runner's `skipped` has no `path_skip` and does not
satisfy a required check.

Repair PR heads (open PRs linked to their repository's open repair ticket) run
all jobs, without paths skips or reuse across different trees by paths contents.
Same-tree reuse remains available. Linking a repair ticket reselects the current
head, replaces service skips and paths reuses with real attempts, and preserves
running attempts.

### Required checks

A job is required by `required = true` in `.guildbotics-ci.toml`. A pull request
answers to the pipeline at the current tip of its **base** branch, so a collecting
branch can drop a job without changing what `main` requires, and a head cannot
take a job off the list by editing its own file. The head still runs its own
definition of each job, so reviews cover changes to a required job's commands.
When the base moves, open pull requests
are judged against the new tip.

| Base's pipeline | Head's pipeline | What the pull request needs |
| --- | --- | --- |
| none | any | nothing |
| invalid | valid, and the merge leaves a valid pipeline | the `required` jobs of the merge result, and the owner's approval of this head (`required_removal` names `pipeline@<base>`, so a new invalid base needs a new approval) |
| invalid, or the branch is gone | anything else | a `pipeline` check that fails: nothing lands |
| requires a job | also marks it `required` | that job on the head |
| requires a job | valid, and merging it would remove or unmark the job | the owner's approval of this head (`required_removal`) |
| requires a job | valid, and the merge keeps the job, which the head has yet to merge | that job: the head merges the base to run it |
| requires a job | missing or invalid | that job, which fails: such a head removes nothing |
| any | invalid, or merging it leaves an invalid file | also a `pipeline` check that fails: an invalid file does not land |

What the merge would leave decides what a head removes: deleting, renaming or
unmarking a required job counts, and a job the merge keeps does not.

An approval counts only from a member of kind owner, on the current head, and only
for the names the head was removing when it was given. A new push, or a base that
makes the same head remove another job, needs a new approval. The approval settles
only those names: the other required checks still have to pass.

### Post-merge only jobs

A job with `post_merge = true` in `.guildbotics-ci.toml` runs only after merge.
It does not run on ordinary branches or PR heads, even before a PR opens, without a
merge base, or when the pipeline changes. Protected branches execute it during
main verification. Repair PR heads execute it before merge too, regardless of paths.
Manual retries also execute it and its skips cannot supply reusable results.

Like required checks, the **base** decides: a PR head follows the pipeline at the tip
of its base for every job the base has, so a head cannot exempt itself from the base's
jobs, and its own pipeline for the jobs it adds, so even the first PR of a release build
can keep it off PR heads. A head without an open PR, or whose base branch is gone,
follows its own pipeline. With several open PRs, a job is skipped only when every base
skips it, and a base with an invalid pipeline skips none. A job cannot be both
`required` and `post_merge`: such a pipeline is invalid.
Selection records the post-merge jobs; a head is reselected when they change.
Newly excluded queued jobs complete as `skipped`, not `cancelled`; running jobs finish
and manual retries remain executable. A job that stops being post-merge only returns
to normal path selection and queues a real attempt when it is selected again.
The log for this decision is `[guildbotics] skipped: post-merge only`.

Repair PR landing requires success of every head job as well as all required
checks. A pending, skipped, or failed optional job blocks publication and merging.
Ordinary PRs require only the required checks of their base.

Head jobs skipped by paths or reusing success by paths are not executed before
landing. Main verification runs them after merge and verifies the combination.
Local merges use the local merge policy; artifact jobs run for the new SHA.

## Releases

A release is a tag on a verified commit of the default branch. Branvil makes
it, on the owner's request or at the pipeline's release times, and records it;
nobody pushes it. Only the recorded tags are releases: the history, the latest
release, and the Hub's updates follow them, and a release made under an earlier
prefix stays one. The pipeline at the tip of the default branch gives the rule:

```toml
[release]                         # optional
tag_prefix = "release/"           # optional; [A-Za-z0-9_./-], at most 32, ends with /, - or .
schedule = ["12:00", "*:30"]      # optional; HH:MM every day, *:MM every hour, up to 24
time_zone = "Asia/Tokyo"          # optional IANA name; default UTC
```

- Tags under the prefix, and every release made, are refused on push for
  every member, the owner included. Other tags keep their rules.
- The owner releases, from the repository's Releases page, a verified commit of
  the first-parent history newer than the latest release. Its name is the
  prefix and either a name the owner gives or `<YYYY-MM-DD>-<n>`, dated in the
  rule's time zone and numbered after the day's existing tags.
- Without `schedule`, releases are made only on request. At each release time,
  Branvil releases every unreleased release point, oldest first, then the newest
  verified commit, when one is newer than the latest release. Each time releases
  once. A schedule seen for the first time, changed (its times or time zone),
  or written again after it was removed waits for its next time. The landing
  sweep (once a minute and on events) checks the times. A time stays unhandled
  while a release point is not verified yet (it and every later release wait)
  or when a release fails part way: the next sweep releases the rest, keeping
  the releases already made.
- An invalid pipeline at the tip keeps the default prefix, makes no automatic
  release, and refuses requests (`release_pipeline_invalid`).

### Release points

A line starting with `::release-point::` in the output of a job of a passing
default-branch verification makes the verified commit a release point; the
rest of the line says why (up to 10 lines per job, 200 characters each). The
runner reads the job's whole output, past the log's limit too and to its end
once the job has exited, and reports the lines with the job's result; they stay with the attempt when its log is pruned
or a backup leaves the logs out, and a result reused from another execution
carries that execution's release points.
No commit newer than an unreleased release point is released before it
(`release_point_first`): the Releases page marks the commit ⚠️ with the
messages and links to the job.

A verification batches the merges since the last verified commit, so a job
finds what a commit introduces by comparing it with `GB_CI_VERIFIED_SHA`
(`verified_sha = true`), not with its parent. GuildBotics' `schema-raise`
prints a release point when a commit raises Branvil's storage format: the Hub
upgrades one step at a time, so each step is released by itself.

## Runs, attempts, and states

A **run** is the CI of one commit of one repository; an **attempt** is one try
of one job (`att_<ULID>`). Checks, probes, and historical attempts share
the job's sequence of attempt numbers.
Checks show the latest `check` attempt of each job: a pull request's page
shows its head's, a commit's page that commit's, and the pull request list's header the default branch tips'
(`GET …/pulls/{number}/checks`, `GET …/commits/{sha}/checks`).

| Attempt status | Conclusion (when completed) |
| --- | --- |
| `queued` → `in_progress` → `completed` (service skips go directly from `queued` to `completed`) | `success`, `failure` (a command exited non-zero), `timed_out`, `cancelled`, `startup_failure` (the commands never started), `skipped` (a service selection decision, or accepted from a runner; this runner does not produce it) |

What registers a run:

- a push of the head of an open pull request (after
  the push is recorded, `Ci::on_ref_changes`), opening or reopening a pull
  request, a merge;
- the reconciler (at start and every minute), which registers every
  running verification SHA, open pull request head again:
  a crash between recording a push and registering its run, or a Git failure
  while reading its pipeline, is repaired, and until then the checks say "No
  CI run is registered for this commit yet" (`queued`, never `no checks`).

A registration always reads the commit's own pipeline, and queues the jobs
that have no current attempt: the latest attempt of that very job
definition. When the run holds another pipeline (one an upgrade could not
verify, or one an older parser read from the same file), it takes the
commit's, and its unfinished attempts of another definition are
stopped. Checks and retries count only
current attempts; an attempt of a job the pipeline dropped or defined
differently stays on its job page as history, never counts as a check, and
cannot be retried.

Rapid pushes: when a branch ref moves from commit A to B and no
other branch ref still wants A, A is **superseded**: its queued
attempts are concluded `cancelled`
("Superseded: …"), its running attempts are asked to stop (the runner sees
`cancel: true` on its next heartbeat), and any result a runner still reports
for A is recorded as `cancelled`. B has its own attempts; a runner may conclude
one successfully by reusing an actual successful execution of the same tree
under the conditions above. A commit that becomes a tip again gets fresh
attempts, eligible for the same claim-time reuse decision. The reconciler repairs
registration after an interrupted branch update.

Leases and interruptions:

- Claiming an attempt gives the runner a lease (`ci.lease_secs`, default 60 s),
  renewed by heartbeats every third of its length. Every runner call carries
  the lease; one that is not the attempt's current, unexpired lease (another
  runner's, an expired one, one of a cancelled or re-queued attempt) is
  `409 conflict` and changes nothing.
- An expired lease (the runner died, lost the service, or the service was
  down) **interrupts** the attempt: it is concluded `cancelled` with the
  details "Interrupted: …", and, the first time for that job and only when it
  was neither cancelled nor superseded, a new attempt is queued in the place
  the interrupted one had in the queue, ahead of the jobs queued after it. A
  second interruption ends that attempt. Eligible protected required checks
  then use the automatic retry budget below; other checks require a manual
  retry.
- A restart of the service is handled the same way: leases are stored, so a
  runner on another machine that keeps working through a short restart keeps
  its attempt, and attempts whose lease ran out while the service was down
  are interrupted by the first sweep after start. The automatic runner
  (`ci.runner = "auto"`) stops with the service: the attempts it held are
  interrupted, and queued again in their place, as soon as its next process
  starts, before that one claims anything.
- A runner that cannot renew its lease before it expires stops the job and
  reports nothing.

Stopping the service:

The integrated Hub's [manual update](../../../docs/hub-server.md#手動更新)
uses `guildbotics server stop`. Branvil keeps serving while GuildBotics
finishes its work and cleanup, continuing to give out CI jobs during that
wait. Only then does it stop giving out jobs and wait for its own running
CI jobs as described below. A forced stop or SIGTERM to the combined server skips the CI wait, including
a force request made while GuildBotics is still finishing its work. On an
orderly stop the combined server logs interrupted jobs and exits
successfully so launchd leaves it stopped.

- For the test server (`test_support::serve`), SIGTERM stops CI first: the service gives out no job to any runner and
  waits until no check runs, at most `ci.stop_wait_secs` (default 3600 s, the
  default job time limit), while it serves everything else, the runners'
  heartbeats, logs, and results among it. Jobs queued meanwhile wait and run
  after the next start. The diagnostics' and the CI settings' runner state is
  `stopping` meanwhile, and the GUI shows the owner a warning on every page; the
  service's log says how many jobs it waits for, and its standard output says
  `branvil stopping: waiting for …`.
- A check still running when that time is up is interrupted ("Interrupted:
  Branvil stopped …; queued again in its place") and queued again in its place,
  so the next start runs it before the jobs queued after it (one already
  interrupted once ends for good, as above); the service then exits with
  status 3.
- SIGINT (Ctrl-C), also during that wait, skips it; the running jobs are
  interrupted as in a restart above.
- Then, after either signal, the GUI's event streams end, the service stops
  accepting requests, and the requests in flight get 5 seconds. The
  repository operations already started (a merge, recording a push,
  following the upstream, publishing a pull request or a poll's work on one)
  finish; none starts meanwhile, and the standard output says
  `branvil stopping: waiting for the started repository operations …` while
  it waits. Operations usually take milliseconds; each Git command and each
  request to the upstream host is bounded by its own time limit. Git runs in
  process groups of its own, so the terminal's Ctrl-C never reaches it, and
  a second Ctrl-C does nothing. To end a stop at once, `kill -9` the service:
  that is safe.
- A service killed before it ends (`kill -9`, a supervisor's deadline such as
  the launchd agent's `ExitTimeOut`, a crash) leaves its Git running; the
  next `serve` waits until that Git has exited (it logs that it waits, and
  SIGINT or SIGTERM ends it then) before it touches a repository, then
  recovers what the killed service did not record, as after a crash
  (`docs/architecture.md`, "Git hosting"). The signals are taken over before
  startup runs any Git, so a signal during startup stops the service once
  startup has finished.

No runner, no executor:

- When no runner has contacted the service for `ci.runner_absent_secs`
  (default 120 s), queued attempts older than that are concluded
  `startup_failure` ("No CI runner has contacted Branvil …"). Its details ask
  the owner to start the runner. Eligible protected-run checks queue an automatic
  retry that waits until a runner claims it; this wait does not exhaust the
  retry budget. Other checks require a manual retry.
- A runner whose executor is unusable advertises no images and leaves attempts
  for other ready runners. When no ready runner has an attempt's image, the same
  timeout concludes it `startup_failure`, naming the missing image and CPU.
  The check includes the runner's CPU, executor or synchronization failure details,
  and actions for supplying the image. See **Settings → CI** for runner diagnostics
  and **Settings → Agent execution environment → Images** to manage Hub images,
  or use `image load --ci` or `runner pull`.

Windows runners report remaining commit headroom from `GlobalMemoryStatusEx`.
Each execution needs enough headroom for the full VM allocation. The Hub
deducts the allocations of claims whose VMs have yet to start. The runner's
`resource_wait` includes the shortfall in MiB when commit headroom is insufficient.

Cancel and retry: `POST …/jobs/{id}/cancel` concludes a queued attempt
`cancelled` at once and asks a running one to stop; `POST …/jobs/{id}/retry`
queues the next attempt number of the latest check attempt, once completed,
with the same job definition. Retry takes the repository lock used for ref changes.
On a current pull request head, the retry form and `guildbotics member checks retry`
accept a runner and repeatable exclusions. Choices have the job's image for their CPU
and match the Hub version. The attempt records the requester and choices. A pinned
attempt waits for that runner and shows whether it is offline, unavailable, busy, or
short of resources. The default branch and other protected branches accept retries
with automatic runner selection.

## Automatic retries

Every job of a protected run automatically retries up to three times
after its initial failure. Eligible endings are command failure, timeout,
runner-reported startup failure, runner absence, and the second interruption of
an attempt. A retry after a runner failure first waits for another runner. The
previous runner may claim it after 30 seconds if no alternative reports
compatible images, the Hub version, and enough capacity. Interruptions and
runner absence keep their existing routing. Attempt and check details identify
a successful execution on a different runner after a failure. The verification
view also shows these recoveries for current and most recently verified work.

The failed attempt and the next queued attempt are recorded in one transaction:
commit checks show the queued retry without an intervening failed check.
Retries keep the same commit, job definition, network setting, and queue
position, and always execute. Each records the first failed attempt and its
automatic retry number. An interruption requeue preserves that record and
number; a manual retry starts a new budget. Job pages and check details show
“Automatic retry 1/3 of att_…”, followed by the attempt's own reason.

After runner absence or a second interruption, the finished attempt names the
automatic retry queued after it. It asks for a manual retry only when none is
queued, including when the automatic retry budget is exhausted.

A retry queued because no runner was available stays queued until a runner
claims it. The attempt history retains all failures and successful retries.
Neither final required-check failures nor automatic recovery send webhook notices.

## Limits

| Limit | Default | Where |
| --- | --- | --- |
| queued attempts | 256 (`ci.queue_max`) | admission counts class 1–3 queued attempts for incoming class 1–3, and all queued attempts for class 4a/4b; a run whose jobs do not fit **waits**, visibly ("Waiting for room in the CI queue"), and waiting runs enter in class order, then registration order; total queued attempts can approach twice the limit, excluding registrations and retries that bypass admission; manual retries use the class after replacing the failed attempt, returning `503 unavailable` if that count is full; interruption recovery and automatic retries keep their old queue place even when new admissions have filled the queue |
| log per attempt | 16 MiB (`ci.log_max_mib`) | then one truncation notice and nothing more; one log request is at most 1 MiB |
| one artifact | 64 MiB (`ci.artifact_max_mib`) | `413 too_large`; at most 16 artifacts per attempt, zip only, names `[A-Za-z0-9_.-]` declared by the job |
| artifacts per attempt (before zipping) | 64 MiB (`ci.executor.artifacts_max_mib`) | runner side |
| job cpus / memory | 4 / 4096 MiB (`ci.executor.max_cpus`, `max_memory_mib`) | every job runs with these |
| work directory quota | 16384 MiB (`ci.executor.work_quota_mib`) | what a job may write to `/work` beyond the checkout and the restored cache; past it, writes fail with `No space left on device` |
| jobs at once | 1 (`ci.executor.max_jobs`) | per runner; the runner claims a job only when it can start it, so the others wait in the service's queue. Jobs at once use that many times the CPUs and memory, and the disk of that many `/work` volumes and job directories (each with the checkout and its archive, and the cache's archive while it is restored or saved) |
| stop wait | 3600 s (`ci.stop_wait_secs`) | how long a stopping service waits for its running jobs ("Stopping the service" above) |
| job time limit | 3600 s (`ci.executor.max_timeout_secs`) | the limit of a job that gives no `timeout_secs`; a larger `timeout_secs` is lowered to it, and the log says so |
| dependency cache | 16 GiB (`ci.executor.cache_max_mib`) | total bytes for pending and published images; see [Dependency cache](#dependency-cache) |
| retention | 30 days (`ci.retention_days`) | logs and artifacts of attempts finished earlier are deleted; the attempt records stay |
| runners per work directory | 1 | a lock file refuses a second runner |

## Logs and artifacts

Logs are files `<data>/ci/logs/<attempt id>.log`, artifacts
`<data>/ci/artifacts/<attempt id>/<name>.zip`; redb records only which
artifacts an attempt has (written after the file is in place). Files that
belong to no attempt are removed at start (orphan collection). An artifact of
a commit is downloaded with `GET …/commits/{sha}/artifacts/{name}` (the newest
check attempt of the commit that has it, resolving reuse to its source).

For a disk job, a separate VM packs the artifacts' paths of `/work` after the
job's VM exits or times out, leaving out paths that go through symbolic links.
With a [RAM workspace](#ram-workspace), the job VM packs the artifacts. The runner
unpacks the archive into the emptied work directory: directories, regular
files, and hard links between them, each
inside the directory (an absolute name lands inside too); a symbolic link is
skipped and noted in the log, and anything else (a FIFO, a sparse file) is
skipped. The job's modes are not kept (files get 0644, directories 0755). It
reads the archive only as a regular file, without following a link or
waiting for a FIFO, and no more of it than the collecting VM's quota, then
collects the artifact paths from the work directory; nothing outside it is
read.

## Dependency cache

A cache entry is a raw ext4 disk image containing the job's cached paths.
On restore, the runner hard-links the published image into the attempt
directory as `cache-base.raw`. The job's `work.qcow2` uses that link as
read-only raw backing and stores guest writes in the overlay. The link
keeps the backing file available until the attempt ends, even if the
published entry is evicted during the job. If either file cannot be set up,
the runner logs the reason and uses an empty named volume for `/work`.

The runner first looks up the exact key. It hashes the repository, job name,
image configuration digest, user ID, work quota, `cache.paths`, and the names
and contents of `cache.key_files`. On a miss it restores the latest published
generation for the same repository, job, user ID, and cache paths. The image
digest, quota, and key files may differ. The job uses the current source and
toolchain, and its work volume can grow to fit the restored image and checkout.
The job log identifies the exact or fallback key, or says there was no entry.
If preparation successfully uses the exact key for `/work`, the runner
skips saving on that attempt. A miss, a fallback generation, or an unusable
exact-key image still allows saving on a successful eligible attempt.

Rust jobs keep dependency artifacts reported by any of their successful Cargo
builds, including fresh units and build script outputs. After
`cargo clean --workspace`, they remove old dependency units and their fingerprints
from `target`. In the Cargo registry cache, they remove downloaded archives and
unpacked source directories for versions absent from `Cargo.lock`, and keep the
registry indexes for dependency resolution.

A successful execution on the head of a PR targeting a protected branch
can keep a pending disk image on its runner. When the Hub records that exact
head as merged into a protected branch, the runner publishes it after the
Hub has accepted the attempt as a success. It discards the image if the head
leaves all eligible PRs before merging. An actual successful execution on a
protected branch can publish after the Hub accepts its result. Reusing a check
after the merge does not schedule a separate execution for the cache.

A pending image never serves a restore. Staging can reclaim superseded and
pending images. The per-runner `cache_max_mib` counts the bytes recorded in
`.branvil-data` for both pending and published images. Size it for at least one
published generation of every cached job plus the largest pending images that
may coexist on the runner. Configure the default or a per-runner override in
**Settings → CI**. The limit does not reserve disk space.

Eviction proceeds from superseded images to pending
images to the current images of each job, oldest use first within each group.
Only publication can evict a current image. Restoring updates the
last-use time only. Generations without matching owner metadata are removed
at runner startup.

## The runner

`guildbotics runner` is a separate process (the same binary). It talks to the
service only over the runner API with the token of a member of kind `runner`;
it never opens the database or the service's repositories (the source arrives
as a Git bundle made by the service, which the runner checks out with the
Git of its machine).

### Join a Windows or Linux runner

How the owner joins a machine is in the user guide,
[Join another machine as a CI runner](../../../docs/code_host.en.md#join-another-machine-as-a-ci-runner).
`guildbotics runner join` verifies and installs msb 0.6.17, stores the runner
credential in the OS keychain, and registers a Windows login task or a systemd
user service at `guildbotics-runner-<port>.service`. Joining again replaces the
previous credential for that device. Work is kept under the machine's
GuildBotics directory, and the runner reads `<machine root>/runner/config.toml`.
The runner checks `/api/v1/runner/health` before claims. When the Hub version
changes, the runner drains its attempts and exits. The login task or user service
restarts it, and the client installs the matching build before it requests work.

### Manual runner maintenance

```sh
# once, on the Hub machine (the owner's token from the data directory): create the runner member, print its token
guildbotics runner register runner1 --url http://127.0.0.1:7878

# once per image the pipelines use, and again to move them to a new version of the tag
guildbotics runner pull docker.io/library/alpine:3.22

guildbotics runner doctor                                          # which msb, executor ready?
GUILDBOTICS_RUNNER_TOKEN=<runner token> guildbotics runner --url http://127.0.0.1:7878
```

Or let the service supervise one: `[ci] runner = "auto"` in `config.toml`
makes the server start `guildbotics runner` as a child (member
`hub-runner`, a token minted at every start and passed through the
environment; the previous automatic token is revoked) and restart it when it
exits. The child reads its work directory (`[ci.executor] work_dir`) and
microsandbox settings (`[ci.microsandbox]`) from the service configuration.

Settings → CI shows every runner member, including those that have not reported
since the Hub started. Reports contain the last contact time, executor state,
version and concurrency. The list also shows held attempts and images from
default-branch pipelines missing from each runner's CPU inventory. A runner
reports on the same cadence when it has no work or is at capacity (normally
every two seconds). The Hub treats reports older than `ci.runner_absent_secs`
as offline and labels their data as the last report. A connected runner with a
ready executor and the Hub build keeps the CI warning clear even when other
runners are offline. If a connected runner has a different build and no runner
can take work, the warning names the incompatible runner even with the
automatic runner switched off.

Select a runner under Settings → CI to save its job resources. The Hub writes
the override to `[ci.runner_limits.<name>]` in its `config.toml` while
preserving the rest of the file. Without an override, the Hub supplies its
`[ci.executor]` defaults. The form shows whether it uses the defaults or
values saved for the selected runner. Every successful claim supplies the resources for
that attempt. Saving new values, including for an offline runner, affects the
next claim and leaves running jobs with their original resources. Each machine
continues to set its own `work_dir`; job resource values in a remote runner's
local file are no longer used.

At each claim the runner reports its logical CPUs, physical memory, and memory
the OS can make available now. An unknown sample leaves resource selection
unrestricted and is shown in Settings → CI. An actual execution records the
VM allocation at claim and its measured peak memory at finish. The estimate
is the nearest-rank 90th percentile of up to 20 recent successful samples for
the same repository, job and VM allocation, or the VM's memory without a
sample. A reusable result on a matching image needs no VM.

The Hub subtracts a physical-memory reserve (default 10%) and the estimates of
held attempts from the machine's capacity. The next attempt must also fit in
currently available memory after the reserve. The CPU budget is logical CPUs
times the configured percentage. The automatic value (0) allows at least the
runner's configured concurrency at its VM size. A later attempt can go ahead
only if it leaves room in both budgets for the first blocked attempt that
could fit on that machine. Settings → CI shows the resource shortfall by
runner for queued work.

Switching the supervised runner off stops its child. Stopping or restarting
the child interrupts the attempts it is running, which are queued once more
(see "Leases and interruptions"); the service removes the microVMs it left.
**Settings → Agent execution environment → Images** manages the supervised runner's
image cache (`<GuildBotics data>/msb`). Another machine can synchronize CPU-specific Hub
images or fill its own cache with `guildbotics runner pull`. Upgrade manually
started runners together with the Hub, as their runner API shares the build's
job and image formats.

Per job the runner:

1. checks the executor (an accepted `msb`, `git` on `PATH`, `msb doctor`;
   cached), logs the attempt with its
   image tag, CPUs, memory, and time limit, then checks the image the cache
   holds under the tag and logs its configuration digest;
2. creates `<work dir>/jobs/<attempt id>/work`, writes the source's bundle
   next to it and clones the bundle into it (Git with an empty global
   configuration and none of the machine's attributes, no reflog), then
   removes the clone's remote and the bundle, sets the clone's Git
   configuration for `/work` ("The job's environment"), and packs the
   checkout into `<work dir>/jobs/<attempt id>/io/checkout.tar`: each file
   Git tracks with the mode and kind the index records, and `.git`. It
   creates the empty `<work dir>/jobs/<attempt id>/log` and its log file
   `out` (opened once, before the VM exists), and checks that no component
   below the runner's directory is a symbolic link. Git never runs in the
   work directory once a VM has had what it holds;
3. when the cache has an entry for the job, hard-links its published
   `disk.raw` to `<work dir>/jobs/<attempt id>/cache-base.raw` under the
   cache read lock and creates `work.qcow2` beside it. The overlay has
   virtual capacity `S` and records `cache-base.raw` as a relative path
   with backing format `raw`. Preparation, the job, and collection all
   attach it at `/work`, with `--mount-disk <work.qcow2>:/work:fstype=ext4`
   on external runners and a disk image mount with filesystem type
   `ext4` in the Hub SDK. With no usable entry, the runner creates a
   named volume (`msb volume create bv-work-<runner>-<attempt> --kind disk
   --size SM`) and attaches it with `--mount-named
   bv-work-<runner>-<attempt>:/work:kind=disk,size=SM`.
   An entry that holds no ext4 file system is not used. When a cached path
   goes through a symbolic link of the commit (unpacking the
   checkout over an entry would write through it), the job neither uses nor
   keeps an entry; the runner decides this from the commit's index, byte
   for byte, not from what its machine's file system made of the checkout,
   so the entry's own links, which a job left there, do not count. It then
   prepares `/work` in a VM of the job's image:
   `msb run --name bv-prep-<attempt> --label branvil.runner=<runner> --label branvil.job=<attempt> --pull never --user 0:0 --mount-dir <io>:/.branvil-io:quota=1M <work mount> --max-duration 3600s --no-net <image> -- /bin/sh -c <grow the file system to the disk; unpack the checkout into /work; chown -R 1000:1000 /work; sync>`,
   removes that sandbox, and runs the job. The file system of an entry is
   grown only when it is smaller than `S` (its size is read from the
   image's superblock). The cache entry is not kept when its file system
   cannot grow, a member of the checkout's archive (`.git` included) is at
   or below a symbolic link of the entry (unpacking would write through the
   link, wherever it points: before it unpacks anything, the VM compares
   `io/checkout.members`, the members' names as the bytes of their headers,
   which the runner writes, with the links `find` lists, whatever their
   number; a `find` that fails, and a link whose path holds a newline
   anywhere, count as links there; a checkout with such a name gets no
   entry), or the checkout does not unpack over the cached paths. The
   runner logs the reason, switches to an empty named volume of the size
   the quota gives (`bv-work-<runner>-<attempt>-new`), and prepares it
   without the entry. A preparing VM that fails on an entry is handled
   the same way. The second preparing VM has a name of its own
   (`bv-prep-new-<attempt>`). An entry's links that no member of the
   checkout goes through stay as they are.
   Every VM of the job flushes before its command ends: microsandbox stops
   the VM when the command ends, and what the guest has not written to
   `/work` or `io` by then can be lost (a lost `chown` left the checkout
   unwritable to the job).
   The job:
   `msb run --name bv-job-<attempt> --label branvil.runner=<runner> --label branvil.job=<attempt> --pull never --cpus N --memory NM --user 1000:1000 <work mount> --mount-dir <log>:/.branvil-log:uid=1000,gid=1000,quota=QM --mount-named bv-tmp-<runner>-<attempt>:/tmp:kind=disk,size=4G --workdir /work --max-duration <timeout+60>s --no-net <image> -- /bin/sh -c <script>`
   (`N` and `M` the runner's CPUs and memory, `S` the size of `/work`, `Q`
   `ci.executor.work_quota_mib`: what the job may write to the host);
   the script first flushes when it exits (`trap sync EXIT`), sends its stdout and stderr
   to `/.branvil-log/out`, and sets `HOME` ("The job's environment"). The job's VM never has the
   `io` directory;
4. streams that file to the log (read every 50 ms through the handle opened
   in step 2, so a link the job puts there is never followed), stops the VM
   when the timeout passes or the service asks to cancel (at the timeout of
   a job that declares artifacts, after `msb exec <job's sandbox> -- sync`,
   as they are collected from the disk: the job runs on while it flushes,
   for at most 30 s, `commands ran for` includes it, a flush that fails is
   logged, and a cancel stops it at once; killing `msb run`
   ends the VM through its parent watchdog, so every process in it stops),
   keeps what the job wrote up to then, and removes its sandbox. A job using a
   [RAM workspace](#ram-workspace) collects artifacts and saves a new cache
   when needed before that VM ends. For a disk job that exits or times out and
   declares artifacts or is eligible to save a new cache image, a VM like the one
   that prepared `/work` (`bv-collect-<attempt>`, started under the shared
   image lock as the job's VM is, a cancel ending the wait for it, `io`
   mounted with `quota=S`) packs the artifacts' paths into
   `io/artifacts.tar`: those that exist and go through no symbolic link (one
   at the end goes as a link), an empty archive when there is none, written
   in large blocks and flushed (`dd … conv=fsync`, so a write that does not
   reach `io` fails). The archive is written as `<archive>.part` and renamed
   once `tar` and `dd` succeeded, so one whose packing failed is not there
   (the log says so). For a cache save, the runner first creates an empty
   disk volume `bv-cache-<runner>-<attempt>` of the same capacity as
   `/work`. The CLI and SDK both mount it at `/.branvil-cache` in the
   collecting VM only. The VM streams the selected `cache.paths` through
   `tar -cf - … | tar -xpf - -C /.branvil-cache`, using the same path
   selection as artifacts and leaving `/work` intact. When both tar
   commands and `sync` succeed, the VM records the KiB used on the new
   file system. After the VM ends, the runner flushes the image file and
   hard-links it to `cache-disk.raw`. The volume image under the
   microsandbox home (`MSB_HOME` on external runners) and the `jobs/`
   and `cache/` subdirectories of the runner's work directory must be on
   the same file system so the runner can link and stage the image. If disk
   creation, the copy inside the VM, or the link fails, the runner logs the
   skipped save. Artifact collection and the job conclusion are independent
   of those failures.
   A failure of the script itself (a `/work` that is not there, a mark that
   cannot be removed, a rename) fails the VM (`set -e`). The artifacts' archive is named only once it was packed
   whole, so it stays even when the VM failed or was stopped later; the
   cache image is discarded then. A stop makes the attempt `cancelled`
   when the job declares artifacts (a stop while only the cache is
   collected leaves its end as it was). It then removes
   every sandbox of the attempt (by its `branvil.job` label) and the
   `/tmp`, `/work`, and cache volumes (which outlive the sandboxes), warning
   about any that could not be removed. The runner also sweeps leftover
   volumes at startup. The overlay and its backing link remain in the
   attempt directory until it is cleaned up. For a job with artifacts, it then
   empties the work directory and unpacks the artifacts' archive into it
   (see "Artifacts"): nothing of the checkout is taken for an artifact;
5. uploads artifacts, stages `cache-disk.raw` if collected after a successful
   eligible run, reports the conclusion with the
   image's configuration digest, and deletes the job's directories.

`S` is `ci.executor.work_quota_mib`, plus twice the size of the checkout's
archive (a disk's blocks and inodes hold many small files in more than the
archive's bytes), 256 MiB, and the bytes of data the cache entry records
(what the image's file takes when it records none),
rounded up to a multiple of 128 MiB (an ext4 block group; microsandbox
refuses a disk whose last, partial group is too small for its metadata), and
no smaller than that image. Its volume is in `MSB_HOME`
(`<GuildBotics data>/msb` on the Hub). `/work` is a
disk rather than the host directory mounted into the VM: microsandbox's
directory mounts read and write a file at a time across the VM's boundary,
which on Windows made the small files of a build about 30 times slower than
on the host (a Rust workspace's build passed its time limit), while a disk
is as fast as the VM's own. What crosses the boundary is one archive at a
time. The image needs `/bin/sh`, `tar`, `chown`, `find`, `dd` (with `obs=`
and `conv=fsync`), `rm`, `mv`, and `sync` for this, `mkdir` when it gives
the job's user no home it can write. For a cache, it also needs `df` and
`awk` to measure what the new disk holds, and `awk`, `find`, `df`, and
`resize2fs` to prepare and grow a restored file system. Without `resize2fs`, an
entry's file system is kept as it is only when it still leaves the quota
(the log says so), and the job runs without the cache otherwise.
Without `sync`, preparing `/work` fails for every job, so an image that ran
jobs before needs it too. See [RAM workspace](#ram-workspace) for the
additional image and kernel requirements for RAM jobs.

The log says how long each step took, so the time of a job can be read off
it: how long the attempt waited in the queue (`queued for … s`, from its
registration to the claim), then `source fetched in … ms` (with the bundle's
size), `checked out in … ms` (the clone and its archive), `cache restored in
… ms` (with the bytes of data of the entry's image), `/work prepared in …
ms` (the VM that unpacks the checkout), `sandbox started in … ms` (from VM creation
to the start of the guest command), `commands ran for … ms`, `/work
collected in … ms` (a job whose artifacts or cache are collected),
`sandbox removed in … ms` (from the job's end, the collecting included),
`artifacts handled in … ms` (a job that declares artifacts), and `cache staged
in … ms` (with the bytes of data in the image). `peak memory: … MiB` is the highest resident memory sample recorded while the
job ran. The Hub uses the SDK's VM metrics. An external runner samples the
`msb run` process tree. A short job can finish before there is a sample.

A reused attempt instead logs `reused result of <attempt> (<commit>, the same
tree), image <digest>` (`the same contents of paths` for the paths basis) and `job ended: success (reused)`. It has no `source fetched`, `sandbox started`, or
`commands ran for` line. `reuse declined` identifies the source and reason
(such as a digest mismatch)
before normal execution; `reuse unavailable: no reuse offered` means the
service supplied no source, including for manual retries and upgraded old
attempts.

At start it removes every sandbox labelled with its own name and every volume
named `bv-tmp-<its name>-<attempt id>`, `bv-work-<its name>-<attempt id>`,
or `bv-work-<its name>-<attempt id>-new` (leftovers of a runner that was
killed), the old job directories, and the
cache entries of an earlier form (a tree or an archive of the cached paths
instead of an image), which no restore reads, and what a save that was
stopped left aside.

## Executor: microsandbox

Why: a microVM per job is a hardware-virtualized boundary (a guest kernel),
stronger than a container sharing the host kernel, and it starts fast (about
0.25 s measured, `docs/performance.md`). GuildBotics already deploys the same
runtime, so no second VM stack is installed.

The Hub boots VMs and streams job output through the microsandbox Rust SDK
(0.6.17). CI and isolated commands share the runtime and images at
`<GuildBotics data>/msb`. The Hub uses the tagged images already in that store.
To make a local archive available to runners of the same CPU on other machines,
run `guildbotics environment image load --ci <archive>` on the Hub.

An external runner uses the pinned `msb` executable. Its configured
`ci.microsandbox.msb_path` takes precedence over the enrolled runtime and the
copy installed by `guildbotics runner install-msb --from <msb>`. On an external
runner, `ci.microsandbox.home` sets `MSB_HOME` (default `<machine root>/runner/msb-home`).
External runners read job output live from the mounted log file.

The job gets private disks at `/work` and `/tmp`. A preparation VM uses the
job's `io` directory to put the checkout into `/work`. On an external runner the
job also gets an empty log directory at `/.branvil-log`. The job cannot access
the host home, Docker socket, code host data, or secrets. By default the Hub
disables the VM network device and external runners pass `--no-net`. With
`ci_network = true` set by the owner, the Public policy allows only public
destinations and blocks the host and private networks. The policy does not
rely on hostname filtering.

### Host prerequisites

| OS | Requirement | Status |
| --- | --- | --- |
| macOS, Apple Silicon | Hypervisor.framework (built in) | **verified** on macOS 26.6 (M1 Pro) with msb 0.6.17 |
| Linux | KVM (`/dev/kvm` readable and writable by the runner's user) | CI |

On every OS: `msb doctor` (run by `guildbotics runner doctor`) must pass, and
`git` must be on the runner's `PATH` (it checks the source out; the executor
reports itself unavailable without it, as without `msb`).

## Testing

- `src/ci/queue.rs`, `src/ci/mod.rs`: the state machine and the checks, on a
  real redb file.
- `tests/ci.rs`: a real service and real Git, with the test acting as the
  runner: a head runs its own pipeline and cannot drop a required check, rapid
  pushes supersede, late and forged results are refused, required checks are
  never success or "no checks" when unregistered, pending, failed, cancelled,
  invalid, or without a runner; leases expire and re-queue once; a restart
  interrupts orphaned attempts; a merge needs success on the exact head.
- `src/runner/mod.rs`: the runner loop against an in-process service with a
  fake executor that starts no VM (results, cancellation, an unusable
  executor, the runner's CPUs, memory, and time limit, the image's digest on
  the attempt and in the log, a work directory that is a checkout of the
  commit with the tags it reaches and no remote, up to `max_jobs` jobs at
  once with the limit supplied by the Hub at claim, a refused token that
  lets the running jobs end before the runner stops (synchronized on the
  executor's `wait`, separately from held source requests that return 401
  and clean up without starting the executor), and a cache that only
  a protected branch's run saves).
- `crates/gb-runner/src/runner/workspace.rs`: the checkout's archive (modes and links from
  the index), the unpacking of what a VM returns (nothing outside its
  directory), and the dependency cache (keys, eviction by the bytes of
  data, entries of an earlier form, and restores and saves of one entry at
  once getting whole images).
- `crates/gb-runner/src/runner/executor.rs`: `msb` run as a fake script (the VMs that prepare
  and collect `/work` as root with the `io` directory, the job's as its user
  without it, the mounts and volumes a job gets, `/work` started from the
  cache entry's image and its image kept for the cache only after a
  collection, collecting after an exit or a timeout and never after a stop,
  their removal, and the sweep of this runner's leftovers only), and what
  the collecting VM keeps for the cache (with the system's `sh` and `find`).
- `crates/gb-runner/src/runner/disk.rs`: copies of an image keep its content
  and its holes and are independent of it.
- `tests/real_executor.rs` (opt-in, `GUILDBOTICS_REAL_EXECUTOR=1`): real microVMs
  (no network or host files; a checkout of the commit at `/work` whose index
  is clean, a non-root user that cannot read a file of mode 0, `/tmp` on a
  disk, the cache restored in the next attempt, nothing left behind).

## 集約用ブランチの確認

子チケットを持つ `ticket/<key>` から既定ブランチへの open Draft PR は、子 PR の
集約先を示す。集約 PR の head を、その PR のマージ判定と同じ必須チェックで確認する。
既定ブランチ側の CI 設定が必須を決め、マージ結果で必須から外れるジョブは修復の対象から外れる。

失敗時は集約先ごとに修復チケットを作り、既定ブランチの修復と同じ規則で担当を選び、
ready の先頭に置く。修復 PR はその集約ブランチへ向かう。修復後の先頭か再試行で
必須チェックが通るとチケットが閉じる。集約 PR をマージせずに閉じて作業を打ち切る
場合は、オーナーが残っている修復チケットで「対応しないでクローズ」を選ぶ。

子 PR の画面と `repository/pr_checks` には、子の head の結果に加えて、base の
失敗したコミット、ジョブ、修復チケットが出る。`hold_while_failing` が有効なら、
確認が回復するまでその集約先への手動・自動マージは修復 PR に限られる。
後続のコミットや再試行の確認中も、直前の失敗は回復が確認できるまで残る。

## 修復チケットの知らせ

既定ブランチまたは集約ブランチの確認が失敗して修復チケットを作るとき、チケットと同じ書き込みで
`repair.notice` に知らせを保存する。webhook へ送る文は次のとおり。

```text
Main verification failed: <repo> <branch> <sha7> (<job>, …)
Range: <verified sha7 or "no verified commit">..<sha7>, <n> pull request(s): !12 !13
Repair ticket: <ticket URL>, assigned to <member name>
```

集約ブランチの場合、1 行目は `Collection branch verification failed` で始まる。

`IX_REPAIR_UNNOTIFIED` のチケットをイベント、起動時、毎分の巡回で読み、
トランザクションの外で送る。送れたら現在のチケットの `notice` を `null` にする。
webhook が未設定または送信失敗なら残して次の巡回で再送する。
通常の再起動では重複しないが、送信成功から保存までに停止すると重複し得る。
開いている修復チケットへの追加の失敗、チケットの終了、通常の PR の失敗、
自動再実行の回復は通知しない。
移行前の修復チケットの `notice` は `null` で、過去の作成は通知しない。

### 確認の状態を見る

PR 一覧の見出しは、既定ブランチの確認の状態を表示する。確認済み、確認中、
失敗、確認待ち、コミットなしを区別し、複数のリポジトリでは状態ごとの数を出す。
引き出しでは確認済みから first-parent で進んだ数、現在の確認のコミットとチェック、
確認済み・確認中・失敗の印を付けたコミットの履歴をページで読める。
自動の流し直しは「確認中（再試行 k/3）」と表示する。

状態の正本は `ci::verify::main_state`。HTTP の `GET …/verification` と
`GET …/verification/commits` もこれを使う。記録の変更後とチェックの更新時には
`verification` イベントを送り、画面の状態と履歴を読み直す。

## Failed main verification and repair

After automatic retries finish, main verification records each failed job and
queues a repair in its result transaction. The autoland sweep collects links,
head-check conclusions, paths skips, post-merge-only jobs (or missing records), the verified ancestor
and commit/PR range, and the last 8 KiB of failed logs. It creates a `branvil`
ticket at the front of `ready`, assigned to the first PR's agent author or the
repository's default responder. Only one open repair exists per repository;
subsequent failures are consumed without another ticket. Evidence tables show at
most 100 commits. When main passes, all pending failures are discarded and open
repairs close as completed in `done`, even if no linked PR merged (the comment
then notes that the failure may be intermittent).


## Failure narrowing

A failed default-branch verification starts one persistent narrowing per failed
job with an attempt, keyed by repository, verified base, and job. Without a base
it starts none; later failures with the same key do not restart it. Verification
failure and repair records are written in the same transaction as the narrowing
intent, initially containing the endpoints and cn's observations. The full history
is fetched afterward, so a diagnostic Git failure cannot hold up the failure or
repair records. Unfinished history reads remain retryable. Diagnostic errors in
the reconciler are logged without skipping ordinary PR head registrations.

A narrowing reads the first-parent history from its verified base c0 through the
failed commit cn. It executes one `probe` at a time, using that commit's own job
definition. cn contributes its verification attempts, including retries. First
c0 runs: two failures mean `outside`, and a failure followed by a success means
`inconsistent`. With no observed failure, two successful probes at cn end the
search as `not_reproduced`. Once a failure is observed, the search bisects
between the last observed passing commit and first observed failure. Each
adjacent boundary commit is tested until it has two results. Mixed results at
one commit or a success after an observed failure mean `inconsistent`;
otherwise `found` records both boundary SHAs. All observations retain their
attempt IDs. An absent job or invalid pipeline counts
as passing with `not_run` and creates no attempt. Two consecutive non-pass/fail
probe outcomes mean `undetermined`, with a reason. A passing main verification
cancels queued probes, requests running probes to stop, and records `abandoned`.
Only the conclusion transaction completes a narrowing; `ci::narrow::concluded`
returns concluded records for repair hints. Check events and the minute reconciler
advance searches while holding the repository lock.

Probes create attempts without registering runs. They never affect commit checks,
verified refs, required checks, failure notifications, landing or member readiness.
They always execute (`allow_reuse=false`) and read published caches,
and never save them. Claim copies cache scope from a run only for `check`; input
digests are copied for every purpose when a run exists. Successful actual probes
can supply tree and paths reuse to checks. Intermediate commits without runs are
absent from both reuse indexes. Probes do not retry automatically or accept manual
retry, survive branch replacement, and recover from interruption like other
attempts. Their job page and logs use the usual routes. Class 2 admission counts
only class 1–3 queued attempts: a full lower-priority queue does not block probes;
when class 1–3 capacity is full, the next event or sweep retries admission.

`landing::held` uses repository `hold_while_failing`, default base, verification
`failing_since`, the open repair ticket and repair PR links from the current
store transaction. Held approved work is normal (3); repair work stays in 2.
Verification success or repair ticket closure restores approved work to 2.
