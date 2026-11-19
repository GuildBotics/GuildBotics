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

Settings → CI and `guildbotics environment image load-ci <archive>` accept archives
for either CPU. The Hub retains the archives under its data directory; only native
images enter its own runner cache. `scripts/build-agent-environment-image.sh` sends
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

All checks must complete, including missing-required-job and invalid-pipeline
markers. All must succeed; no pipeline and no required checks also passes.
Required checks govern publication and merging on PR heads; in main verification
they require that those names exist, while *all* pipeline jobs determine success.
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

Verification attempts normally use class **4m**, after merge-directed work
(classes 1–3 and 4a), before drafts and cache refresh (4b). They display
`Deferred: main is verified after the jobs that lead to a merge.` They do not
consume admission capacity for any class. If `failing_since` is set, or any attempt
of the running verification has failed, timed out, lacked a runner, or exhausted
interruption recovery, all verification jobs use class **1** and count in ordinary
class 1–3 admission. Other protected branches always retain class 1.
This batches merges without a fixed delay while main is healthy; when it might
be broken, verification and repair become part of the path to merging.

Jobs declaring `verified_sha = true` record the latest verified SHA when claimed,
return it in the claim response, and receive `BRANVIL_VERIFIED_SHA` (empty before
any success). Reuse for these jobs requires both the same verified SHA and the
same commit tree, even when `paths` match: ancestry of a verified commit is not a
property of those paths. Undeclared jobs retain the usual reuse rules and have
no such environment variable. Protected registration reruns unsuccessful attempts
previously executed for PRs; successful cacheless actual executions remain valid.

## The pipeline: `.branvil/ci.toml`

```toml
version = 1                       # optional; only 1 exists

[[jobs]]
name = "test"                     # [A-Za-z0-9_.-]{1,64}, not "pipeline", unique
image = "docker.io/library/alpine:3.22"   # [registry/]name[:tag], no digest
commands = ["./scripts/test.sh", "make lint"]   # 1-64 commands, each run by /bin/sh -c
paths = ["src/**", "tests/*.rs", "Cargo.lock"]  # optional; empty/omitted runs every change
verified_sha = false             # optional; declares BRANVIL_VERIFIED_SHA as an input
timeout_secs = 600                # optional, 1-86400; default the runner's time limit
artifacts = [{ name = "report", paths = ["out/report/", "coverage.xml"] }]   # up to 16
cache = { paths = ["target"], key_files = ["Cargo.lock"] }                    # optional
```

- Commands run in order in `/work`; the first one that fails ends the job
  with its exit code. The job's environment has only CI metadata (besides
  the image's own variables and `HOME`): `CI=true`, `BRANVIL_CI=true`,
  `BRANVIL_REPOSITORY`, `BRANVIL_SHA`, `BRANVIL_JOB`, `BRANVIL_ATTEMPT`,
  `BRANVIL_ATTEMPT_ID`. Jobs declaring `verified_sha = true` also receive
  `BRANVIL_VERIFIED_SHA` (the latest verified commit at claim, or empty). No tokens, no host variables. See "The job's
  environment" for `/work`, the user, and `/tmp`.
- Jobs of one run are queued together; a runner runs queued jobs, of any
  run, up to `ci.executor.max_jobs` at once, each in a microVM of its own
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
  retries, interrupted attempts, and cache refreshes keep their existing rules.
  At each claim the service derives a class from current store records, without
  Git or a saved priority. The first matching rule wins:

  | Scope | Condition | Class |
  | --- | --- | --- |
  | Attempt | Cache refresh | 4b (last) |
  | Attempt | Probe (even with a protected run, or without a run) | 2 |
  | Run | Missing run or repository, or no branches want the commit | 4b |
  | Branch | Protected, except healthy default-branch verification (4m) | 1 |
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
  4a/4b distinguish cache refresh, missing records, unwanted commits, no open
  PR, drafts, the failed required check, and the reviewer requesting changes
  (with the approval count).

  Queued check and job `details` show the deferred reason and when the job can
  run for classes 4a/4b, after any automatic retry explanation. Waiting runs
  append the deferred reason to their queue-capacity message.

  Claim order is 1, 2, 3, 4a, 4m, 4b. Within a class, attempts with an available
  reusable successful result (of the same tree, or of the same contents of
  paths; "Trust") come first, then registration sequence (`seq`);
  without a reusable result, sequence decides. Reuse is assessed after taking
  the run's current cache scope; retries with `allow_reuse = false` execute.
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
  `guildbotics runner pull` or Settings → CI (pull a tag from a registry, or load
  a `docker save` / OCI archive from a file on the Branvil machine). To move
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
  loads (Settings → CI, `guildbotics runner pull`) hold a lock of the image cache
  (a file of `MSB_HOME`) exclusively, and a job holds it shared from checking
  its image until its VM has started, so a load that re-points the tag waits
  for the jobs that are starting and never changes the image a checked job
  runs (with several jobs at once, a pull or a load waits for a moment when
  no job is starting); a job that waits for a pull or a load says so in its
  log and can be cancelled meanwhile (`msb` used by hand does not take the
  lock). Settings → CI shows the tag to write and the digest of
  every cached image. (A runner started by hand on another machine loads such
  an archive with `msb load` under its own `MSB_HOME`.)
- A pull or a load also names the version the tag now points to by its
  manifest digest (`<name>@sha256:…`), as msb cannot remove a version that no
  reference names. After a pull or a load, and before each job, the runner
  removes the versions no tag points to anymore, with their layers; msb
  refuses one a job still runs on, which goes at a later job's start. These
  digest references are no images a pipeline can name, and Settings → CI does
  not show them. A load pins the reference it changed in the cache, the name
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
- **`/tmp` is on a disk, not in the VM's memory**: an ext4 disk of 4 GiB of
  the job's own (mode 1777), a named volume `bv-tmp-<runner>-<attempt id>`
  the runner removes after the job. (Without it, microsandbox puts `/tmp` in
  a tmpfs of a quarter of the VM's memory, at most 512 MiB.) The VM's root
  disk (Branvil passes no `--root-disk`) and `/tmp` are microsandbox's 4 GiB
  default. `/work` is an ext4 disk of the job's own too, a named volume
  `bv-work-<runner>-<attempt id>`: what the job may add to it beyond the
  checkout and the restored cache is `ci.executor.work_quota_mib` (16 GiB by
  default; a workspace built from scratch outgrows microsandbox's 4 GiB
  default). They are the runner's, not the pipeline's.

## Trust: where the pipeline comes from

Every commit runs its own pipeline: a tip of the default branch and the head
of an open pull request alike read `.branvil/ci.toml` from that commit. A pull
request that changes the pipeline runs the changed pipeline as its checks, so
a mistake in it shows before the merge. Main verification checks the combination after merge.
PR-head jobs with `paths` run if any matching file changed since the
merge-base with the current base tip of any open PR sharing that head SHA
(and may then reuse a success with the same contents of paths, below).
A changed `.branvil/ci.toml`, a missing base, or no merge-base runs all jobs.
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
metadata and a `[branvil] skipped: no file matching paths changed since …`
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

### Post-merge only jobs

The repository CI settings expose `post_merge_jobs`, a list of job names (empty by
default). These jobs do not run on ordinary branches or PR heads, even before a PR
opens, without a merge base, or when the pipeline changes. Protected branches execute them during main verification. Repair PR heads execute
them before merge too, regardless of paths.
Manual retries also execute them and their skips cannot supply reusable results.

The owner controls this in repository settings rather than `.branvil/ci.toml` so a
PR cannot exempt its own jobs. Required checks and post-merge jobs cannot overlap.
Selection records include the setting; reconciliation reselects open heads when
it changes. Newly excluded queued jobs complete as `skipped`, not `cancelled`;
running jobs finish and manual retries remain executable. Removing a name restores
normal path selection and queues a real attempt when it is selected again.
The log for this decision is `[branvil] skipped: post-merge only (repository setting)`.

Repair PR landing requires success of every head job as well as all required
checks. A pending, skipped, or failed optional job blocks publication and merging.
Ordinary PRs continue to require only their configured required checks.

Head jobs skipped by paths or reusing success by paths are not executed before
landing. Main verification runs them after merge and verifies the combination.
Local merges use the local merge policy; artifact jobs run for the new SHA.

## Runs, attempts, and states

A **run** is the CI of one commit of one repository; an **attempt** is one try
of one job (`att_<ULID>`, numbered 1, 2, … per job). An attempt's `purpose`
is `check`, `cache_refresh`, or `probe`; all purposes share the job's attempt numbers.
For example, check #1 followed by refresh #2 makes a manual check retry #3.
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
  retry. Cache refreshes need no manual retry: a later check requests a new
  refresh if the cache still needs one.
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

- For standalone Branvil, SIGTERM stops CI first: the service gives out no job to any runner and
  waits until no check runs, at most `ci.stop_wait_secs` (default 3600 s, the
  default job time limit), while it serves everything else, the runners'
  heartbeats, logs, and results among it. Jobs queued meanwhile wait and run
  after the next start. The diagnostics' and the CI settings' runner state is
  `stopping` meanwhile, and the GUI shows the owner a warning on every page; the
  service's log says how many jobs it waits for, and its standard output says
  `branvil stopping: waiting for …` when running Branvil on its own.
- Cache-refresh attempts are interrupted immediately when stopping starts;
  they do not use the wait period. The first interruption requeues them with
  their purpose and cache key. A second interruption ends them and releases
  the key for a later check's refresh request. Checks use the wait period.
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
  retry budget. Other checks require a manual retry; cache refreshes explain
  that a later check can request another refresh.
- A runner whose executor is unusable advertises no images and leaves attempts
  for other ready runners. When no ready runner has an attempt's image, the same
  timeout concludes it `startup_failure`, naming the missing image and CPU.
  The check includes the runner's CPU, executor or synchronization failure details,
  and actions in Settings → CI / `load-ci` / `runner pull`. The same details remain
  visible in runner diagnostics.

Cancel and retry: `POST …/jobs/{id}/cancel` concludes a queued attempt
`cancelled` at once and asks a running one to stop; `POST …/jobs/{id}/retry`
queues the next attempt number of the latest check attempt, once completed,
with the same job definition (the pipeline is not read again). Cache refreshes
cannot be retried. Retry takes the repository lock used for ref changes.

## Automatic retries

Every job of a protected run automatically retries up to three times
after its initial failure. Eligible endings are command failure, timeout,
no runner contacting the service, and the second interruption of an attempt.
Other startup failures (such as a missing image), requested cancellations,
supersession, pull-request runs, and cache refreshes do
not trigger automatic retries.

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
| dependency cache | 2048 MiB (`ci.executor.cache_max_mib`) | superseded generations are removed before current entries; least recently used first within each group |
| retention | 30 days (`ci.retention_days`) | logs and artifacts of attempts finished earlier are deleted; the attempt records stay |
| runners per work directory | 1 | a lock file refuses a second runner |

## Logs and artifacts

Logs are files `<data>/ci/logs/<attempt id>.log`, artifacts
`<data>/ci/artifacts/<attempt id>/<name>.zip`; redb records only which
artifacts an attempt has (written after the file is in place). Files that
belong to no attempt are removed at start (orphan collection). An artifact of
a commit is downloaded with `GET …/commits/{sha}/artifacts/{name}` (the newest
check attempt of the commit that has it, resolving reuse to its source).

A VM packs the artifacts' paths of `/work` once the job's VM ended, by exit
or by timeout ("The runner"), leaving out a path that goes through a
symbolic link, and the runner unpacks that archive into the emptied work
directory: directories, regular files, and hard links between them, each
inside the directory (an absolute name lands inside too); a symbolic link is
skipped and noted in the log, and anything else (a FIFO, a sparse file) is
skipped. The job's modes are not kept (files get 0644, directories 0755). It
reads the archive only as a regular file, without following a link or
waiting for a FIFO, and no more of it than the collecting VM's quota, then
collects the artifact paths from the work directory; nothing outside it is
read.

## Dependency cache

`cache.paths` are restored into `/work` before the job, and saved after a
successful job of a run that a protected branch asked for (its cache scope is
`protected`). An entry is the archive a VM packed of the cached paths, links
and hard links, owners and modes included; a VM unpacks it into `/work`, and
the runner never unpacks it. When a protected branch comes to want a commit whose
jobs ran for a pull request, jobs with a cache get
protected attempts (one still running for the pull request is stopped), and an
attempt still queued takes its run's scope when a runner claims it. These
check attempts execute only when they cannot reuse a result. Reused protected
checks request separate cache-refresh attempts when their cache needs a save.
The service records the successful check and enqueues its refresh in the same
transaction. At most one queued or running refresh exists per repository,
job name, and cache key, even across commits. A full queue skips the refresh;
a later request can enqueue it. The check links to the new or existing refresh.

Refresh attempts always execute in protected scope and save after success.
They never appear in commit checks, required checks, readiness;
their waiting, execution, or failure therefore does not delay automatic updates.
They survive branch movement. Successful refreshes can supply reuse results
and duration estimates. They share queue limits and file retention with checks.
Their pages identify them as cache refreshes; manual retry is only for checks.
If an image tag changes before execution, the refresh computes and saves the
key for the image it actually runs.

A pull request's run (`branch:<name>`) restores the same entries and save nothing (their logs
say `cache not saved`): they cannot plant a cache entry that the default branch
would use, and never push out the entries every run restores. The key is the SHA-256
of the repository, the job name, the configuration digest of the image the
job runs on (a new version of the tag does not reuse what the previous one
built), the user the job runs as (the archive keeps the owner of each file),
the cached paths, and the content of `cache.key_files`. The cache lives in
`<runner work dir>/cache/`, an entry's archive in `<key>/cache.tar`. A
restore copies the archive into the job's `io` directory, which the
preparing VM can write, so no VM ever has an entry's own file; a save copies
the new archive in, read as the artifacts' archive is (a regular file, no
link followed, no FIFO waited for), and replaces the entry, which is never
changed in place.

A job finds no entry (`no cache entry yet`) when no protected branch's run
has saved one for its key yet, or the entry was evicted. Changing a lock file
or image version needs a matching protected save before pull requests can
restore it. A pull request with an older lock file can still restore an older
generation while that entry remains in the cache.
The cache can hold multiple generations for each repository and job the runner
serves. Set `cache_max_mib` to at least the total size of one generation of all
cached jobs: below that, even current entries can be evicted. Extra capacity
keeps older generations for pull requests that have not taken a lock-file
change and for protected branches with different lock files.

Each complete save records its time alongside the cached contents before
they replace the old entry. Restoring updates the last-use time for eviction
but never the save time. A protected reused job requests a refresh when its
save is more than 24 hours old (`CACHE_REFRESH_SECS`); a missing save timestamp,
an evicted entry, a future timestamp, or an unreadable save also requests a
refresh. A matching tree and image imply the
same key, so this check uses the source execution's recorded key without
fetching or checking out the source.

Each entry records its repository and job alongside its contents, in the same
temporary directory that replaces the entry on save. For each repository and
job, the entry just saved is current, even if another generation has a later
save timestamp (the timestamp is recorded before acquiring the write lock).
For other repository/job pairs, the latest save timestamp selects the current
entry, with the greatest cache key breaking ties. Entries without a save
timestamp are superseded.

When the byte limit is exceeded, eviction removes superseded generations
first, then current entries only after no superseded generations remain.
Restoring and saving both explicitly record the last-use time. Within each
of these two groups, eviction removes the oldest last-use timestamps first.
For equal timestamps, the entry just saved is removed last; other entries
are removed in cache-key order, regardless of size.
The byte limit still applies to the saved entry: an entry larger than the limit
is removed too.

Protected branches with different lock files share a repository/job pair:
the last save becomes current and the other generation becomes superseded.
The same applies when an older commit's save or retry finishes after a newer
commit's save. Both generations remain while capacity permits; under pressure
the superseded one goes first. Entries of deleted or renamed repositories and
jobs remain current because nothing supersedes them. They are evicted only
after all superseded entries, when the limit still requires it.

On the first save after upgrading to a runner that records each entry's
repository and job, entries without that record are removed even below the
limit. Each cached job must rebuild its cache once after the upgrade.
Entries with unreadable or invalid records are also removed on the next save,
so a damaged record cannot stop eviction of other entries.

## The runner

`guildbotics runner` is a separate process (the same binary). It talks to the
service only over the runner API with the token of a member of kind `runner`;
it never opens the database or the service's repositories (the source arrives
as a Git bundle made by the service, which the runner checks out with the
Git of its machine).

### Join a Windows runner

Install the GuildBotics client and keep SSH forwarding to the Hub running first.
Run `guildbotics runner join` and approve **CI runner** access in the browser.
The command verifies and installs msb 0.6.17, stores the runner credential in the
OS keychain, and registers a per-user login task. It never prints the credential.
Joining again replaces the previous credential for that device. Work is kept
under the machine's GuildBotics directory, outside the Windows temporary directory.
The runner checks `/api/v1/runner/health` before claims. A changed Hub version
stops new claims, drains running attempts, and exits so the task restarts it;
the client updater installs the matching Hub build before it resumes.

### Manual runner maintenance

```sh
# once, on the Hub machine (the owner's token from the data directory): create the runner member, print its token
guildbotics runner register runner1 --url http://127.0.0.1:7878

# once per image the pipelines use, and again to move them to a new version of the tag
guildbotics runner pull docker.io/library/alpine:3.22

guildbotics runner doctor                                          # which msb, executor ready?
BRANVIL_TOKEN=<runner token> guildbotics runner --url http://127.0.0.1:7878
```

Or let the service supervise one: `[ci] runner = "auto"` in `branvil.toml`
makes the server start `guildbotics runner` as a child (member
`branvil-runner`, a token minted at every start and passed through the
environment; the previous automatic token is revoked) and restart it when it
exits. The child reads the service's configuration file for its
`[ci.executor]` and `[ci.microsandbox]` settings.

The owner changes the mode and the job resources under Settings → CI
(`docs/api.md`, "CI settings") without restarting the service: they are
written to `branvil.toml` (the rest of the file is kept as it is), switching
the mode off stops the child, and the runner reads the limits from the file
again before every claim, so new limits apply from the next job it starts and
the running jobs keep theirs (a runner started by hand reads its own
configuration file the same way). Stopping or restarting the child interrupts
the attempts it is running, which are queued once more (see "Leases and
interruptions"); the service then removes the microVMs the child left. The
child refuses to start when it cannot read the configuration file. The image
cache Settings → CI
manages is the supervised runner's (`[ci.microsandbox] home`); a runner
started by hand on another machine keeps its own, filled with
`guildbotics runner pull`. A runner started by hand is upgraded together with
the service: the runner API carries the job definition and the image digest
in this version's shapes.

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
3. puts a copy of the cache entry's archive at `io/cache-in.tar`, then prepares `/work`
   in a VM of the job's image:
   `msb run --name bv-prep-<attempt> --label branvil.runner=<runner> --label branvil.job=<attempt> --pull never --user 0:0 --mount-dir <io>:/.branvil-io:quota=1M --mount-named bv-work-<runner>-<attempt>:/work:kind=disk,size=SM --max-duration 3600s --no-net <image> -- /bin/sh -c <unpack the archives into /work; chown -R 1000:1000 /work; sync>`,
   removes that sandbox and `io/cache-in.tar`, and runs the job. The cache entry is not
   unpacked when a cached path goes through a symbolic link of the commit
   (it would be written through) or the archive does not unpack: `/work` is
   then the checkout alone, and the log says the job runs without the cache.
   Every VM of the job flushes before its command ends: microsandbox stops
   the VM when the command ends, and what the guest has not written to
   `/work` or `io` by then can be lost (a lost `chown` left the checkout
   unwritable to the job).
   The job:
   `msb run --name bv-job-<attempt> --label branvil.runner=<runner> --label branvil.job=<attempt> --pull never --cpus N --memory NM --user 1000:1000 --mount-named bv-work-<runner>-<attempt>:/work:kind=disk,size=SM --mount-dir <log>:/.branvil-log:uid=1000,gid=1000,quota=QM --mount-named bv-tmp-<runner>-<attempt>:/tmp:kind=disk,size=4G --workdir /work --max-duration <timeout+60>s --no-net <image> -- /bin/sh -c <script>`
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
   keeps what the job wrote up to then, and removes its sandbox. Once the
   job's VM ended by exit or timeout, and the job declares artifacts or
   succeeded in a protected branch's run with a cache, a VM like the one
   that prepared `/work` (`bv-collect-<attempt>`, started under the shared
   image lock as the job's VM is, a cancel ending the wait for it, `io`
   mounted with `quota=` twice `S`, as
   the artifacts and the cache may pack the same files) packs the artifacts' paths into
   `io/artifacts.tar` and the cached paths into `io/cache.tar`: those that
   exist and go through no symbolic link (one at the end goes as a link),
   an empty archive when there is none, written in large blocks and flushed
   (`dd … conv=fsync`, so a write that does not reach `io` fails). Each is
   written as `<archive>.part` and renamed once `tar` and `dd` succeeded, so
   an archive whose packing failed is not there (the log says so); a failure
   of the script itself (a `/work` that is not there) fails the VM (`set -e`).
   Both archives are dropped when the VM failed or was stopped; a stop
   makes the attempt `cancelled`. It then removes
   every sandbox of the attempt (by its `branvil.job` label) and the `/tmp`
   and `/work` volumes (which outlive the sandboxes), warning only about one
   that exists and could not be removed, and, for a job with artifacts,
   empties the work directory and unpacks the artifacts' archive into it
   (see "Artifacts"): nothing of the checkout is taken for an artifact;
5. uploads artifacts, saves `io/cache.tar` as the cache entry after a
   success of a protected branch's run, reports the conclusion with the
   image's configuration digest, and deletes the job's directories.

`S` is `ci.executor.work_quota_mib`, plus twice the size of the archives
unpacked into `/work` (a disk's blocks and inodes hold many small files in
more than the archive's bytes) and 256 MiB, rounded up to a multiple of
128 MiB (an ext4 block group; microsandbox refuses a disk whose last, partial
group is too small for its metadata). Its volume is in `MSB_HOME`
(`[ci.microsandbox] home`), not in the runner's work directory. `/work` is a
disk rather than the host directory mounted into the VM: microsandbox's
directory mounts read and write a file at a time across the VM's boundary,
which on Windows made the small files of a build about 30 times slower than
on the host (a Rust workspace's build passed its time limit), while a disk
is as fast as the VM's own. What crosses the boundary is one archive at a
time. The image needs `/bin/sh`, `tar`, `chown`, `find`, `dd` (with `obs=`
and `conv=fsync`), `rm`, `mv`, and `sync` for this, and `mkdir` when it gives the job's user no home it can write.
Without `sync`, preparing `/work` fails for every job, so an image that ran
jobs before needs it too.

The log says how long each step took, so the time of a job can be read off
it: how long the attempt waited in the queue (`queued for … s`, from its
registration to the claim), then `source fetched in … ms` (with the bundle's
size), `checked out in … ms` (the clone and its archive), `cache restored in
… ms` (with the size of the entry's archive), `/work prepared in … ms` (the
VM that unpacks the archives), `sandbox started in … ms` (the spawn of the
job's `msb run` → the script's first line), `commands ran for … ms`, `/work
collected in … ms` (a job whose artifacts or cache are collected),
`sandbox removed in … ms` (from the job's end, the collecting included),
`artifacts handled in … ms` (a job that declares artifacts), and `cache saved
in … ms` (with the archive's size). `peak memory of msb run and its VM: … MiB` is
the largest resident memory of `msb run` and every process under it (the VM
among them), read with `ps` every second while the job runs; the VM's grows
with the guest memory the job touches and does not shrink, so the last samples
hold the peak. The line is emitted only when a positive sample was collected.
A host without `ps`, or a short-lived process that exits before `ps` sees it,
gives no such line; an absent measurement is not reported as zero MiB.

A reused attempt instead logs `reused result of <attempt> (<commit>, the same
tree), image <digest>` (`the same contents of paths` for the paths basis) and `job ended: success (reused)`, with the cache key
and save age when checked. It has no `source fetched`, `sandbox started`, or
`commands ran for` line. `reuse declined` identifies the source and reason
(digest mismatch, missing cache key/save, or save older than 86400 seconds)
before normal execution; `reuse unavailable: no reuse offered` means the
service supplied no source, including for manual retries and upgraded old
attempts. These lines distinguish reused work from cache refreshes when
measuring queue time and execution time.

At start it removes every sandbox labelled with its own name and every volume
named `bv-tmp-<its name>-<attempt id>` or `bv-work-<its name>-<attempt id>`
(leftovers of a runner that was killed), the old job directories, and the
cache entries of an earlier form (a tree of the cached paths instead of an
archive), which no restore reads.

## Executor: microsandbox

Why: a microVM per job is a hardware-virtualized boundary (a guest kernel),
stronger than a container sharing the host kernel, and it starts fast (about
0.25 s measured, `docs/performance.md`). GuildBotics already deploys the same
runtime, so no second VM stack is installed.

How it is driven: through the `msb` command line, version **0.6.17 exactly**
(the runner checks `msb --version`). The Rust SDK crate was the first choice,
but its 0.6.17 release cannot be built without downloading a guest-agent
binary at build time, which Branvil's build does not do; the CLI needs no
build-time download and no SDK dependency.

`msb` is looked up in this order, each accepted only with the pinned version:
`ci.microsandbox.msb_path`, the embedding application's configured runtime
binary (GuildBotics supplies its deployed runtime), Branvil's copy
`~/.branvil/msb/bin/msb`
(created only by `guildbotics runner install-msb --from <msb>`). Nothing is
downloaded. Branvil's microsandbox state (`MSB_HOME`: image cache, sandbox
records) is `~/.branvil/msb-home` (`ci.microsandbox.home`), separate from
GuildBotics'. `msb` runs with only `PATH`, `HOME`, `USER`, `LOGNAME`, `TMPDIR`,
`LANG` from the host, plus `MSB_HOME` and an
empty `DOCKER_CONFIG` (host registry credentials are never read).

Why the log goes through a file: `msb run` 0.6.17 hands a command's stdout
and stderr over only when the command exits (measured: lines written at 0 s
and at 2 s arrive together when the command ends; `--tty` and `--timeout` do
not change it, and a killed `msb run` delivers nothing). Read from `msb run`,
a running job's log would stay empty, and a job stopped by its timeout or a
cancellation would lose all its output.

What a job gets: its own empty log directory at `/.branvil-log` as its only
host mount, and disks of its own at `/work` (the checkout, prepared by a VM
that has the job's `io` directory) and `/tmp`; no host home, no Docker
socket, no Branvil data, no secrets. Network: denied
(`--no-net`: the guest has an interface, but the host-side network stack
refuses every connection and DNS answers nothing; verified to a public host
name, a public IP, and the host gateway). With `ci_network = true` (the owner
only) a job gets `--net public`: public destinations only, never the host or
private networks; microsandbox's hostname filtering is best effort and not
used as a guarantee.

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
  once with the limit read before every claim, a refused token that
  lets the running jobs end before the runner stops (synchronized on the
  executor's `wait`, separately from held source requests that return 401
  and clean up without starting the executor), and a cache that only
  a protected branch's run saves).
- `crates/gb-runner/src/runner/workspace.rs`: the checkout's archive (modes and links from
  the index), the unpacking of what a VM returns (nothing outside its
  directory), and the dependency cache (keys, eviction, entries of an
  earlier form, and restores and saves of one entry at once getting whole
  archives).
- `crates/gb-runner/src/runner/executor.rs`: `msb` run as a fake script (the VMs that prepare
  and collect `/work` as root with the `io` directory, the job's as its user
  without it, the mounts and volumes a job gets, collecting after an exit or
  a timeout and never after a stop, their removal, and the sweep of this
  runner's leftovers only).
- `tests/real_executor.rs` (opt-in, `BRANVIL_REAL_EXECUTOR=1`): real microVMs
  (no network or host files; a checkout of the commit at `/work` whose index
  is clean, a non-root user that cannot read a file of mode 0, `/tmp` on a
  disk, the cache restored in the next attempt, nothing left behind).

## 修復チケットの知らせ

main の確認が失敗して修復チケットを作るとき、チケットと同じ書き込みで
`repair.notice` に知らせを保存する。webhook へ送る文は次のとおり。

```text
Main verification failed: <repo> <branch> <sha7> (<job>, …)
Range: <verified sha7 or "no verified commit">..<sha7>, <n> pull request(s): !12 !13
Repair ticket: <ticket URL>, assigned to <member name>
```

`IX_REPAIR_UNNOTIFIED` のチケットをイベント、起動時、毎分の巡回で読み、
トランザクションの外で送る。送れたら現在のチケットの `notice` を `null` にする。
webhook が未設定または送信失敗なら残して次の巡回で再送する。
通常の再起動では重複しないが、送信成功から保存までに停止すると重複し得る。
開いている修復チケットへの追加の失敗、チケットの終了、必須チェックだけの失敗、
自動再実行の回復は通知しない。自動再実行の回数と順、着地の通知は変わらない。
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
`inconsistent`. Otherwise the search bisects between the last observed passing
commit and first observed failure. Adjacent boundary commits are tested until
each has two results. Mixed results at one commit or a success after an observed
failure mean `inconsistent`; otherwise `found` records both boundary SHAs. All
observations retain their attempt IDs. An absent job or invalid pipeline counts
as passing with `not_run` and creates no attempt. Two consecutive non-pass/fail
probe outcomes mean `undetermined`, with a reason. A passing main verification
cancels queued probes, requests running probes to stop, and records `abandoned`.
Only the conclusion transaction completes a narrowing; `ci::narrow::concluded`
returns concluded records for repair hints. Check events and the minute reconciler
advance searches while holding the repository lock.

Probes create attempts without registering runs. They never affect commit checks,
verified refs, required checks, failure notifications, landing or member readiness.
They always execute (`allow_reuse=false`), read caches with `cache_scope=probe`,
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
