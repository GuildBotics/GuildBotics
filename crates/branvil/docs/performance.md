# Performance

Measured numbers only; every figure names its conditions. Nothing here is a
target that was declared met without a measurement.

The parts are measured separately: the service ([port operations](#service-port-operations),
[startup, memory, and files](#startup-memory-and-files)), [Git](#git-over-smart-http),
the [GUI](#gui), and the [CI runner and its executor](#ci-executor);
[totals](#totals) puts them together.

## How to run

The load tool is `examples/bench.rs` (not part of the service binary). It
starts `branvil serve` itself, seeds through the HTTP API and Git Smart HTTP
only (never by writing redb), and measures copies (APFS clones) of the seeded
data, so every run starts from the same state.

```sh
cargo build --release
B=/some/empty/dir
cargo run --release --example bench -- seed    --work $B   # 10,000 tickets, 2,000 PRs, 50,000 comments (all sizes are options)
cargo run --release --example bench -- run     --work $B --cache-mib 32 --concurrency 1,8,32 --requests 200
cargo run --release --example bench -- startup --work $B --repeat 5
cargo run --release --example bench -- git     --work $B
```

Results go to `$B/results-*.json` and a Markdown summary to stdout. The Git
clients it starts use `GIT_CONFIG_NOSYSTEM=1`, an empty `GIT_CONFIG_GLOBAL`,
`GIT_TERMINAL_PROMPT=0`, `-c credential.helper=`, and credentials only through
a `GIT_ASKPASS` script that reads the environment. The GUI timing is
`web/e2e/perf.spec.ts` (see [GUI](#gui)).

## Conditions

| | |
| --- | --- |
| Machine | Apple M1 Pro (8 performance + 2 efficiency cores), 16 GiB, macOS 26.6.2 (build 25G83) |
| Storage | internal SSD (APFS, Apple Fabric); the data directory on the system volume |
| Build | `cargo build --release` (Rust 1.96.0, `lto = "thin"`, `codegen-units = 4`); Git 2.54.0 (Apple Git-157) for the service and the clients |
| redb | 4.4.0, durability `Immediate` (redb's default: `commit()` returns after the file is synced; Rust's `sync_data` issues `F_FULLFSYNC` on macOS), cache 32 MiB unless stated, `read_concurrency` 8, one writer |
| Service limits | defaults: `git_concurrency` 8, `git_http_concurrency` 8 |
| CI | `ci.runner_absent_secs` raised so the seeded queue stays as it is; no runner during the measurements (attempts were concluded during seeding by the load tool acting as a runner, without an executor) |
| Clients | the load tool on the same machine, keep-alive HTTP/1.1, closed loop: *c* workers each send the next operation when the previous one answered; 200 operations per row |
| Background | not an idle machine: other builds and agents ran meanwhile; load average 4–13 during the runs (each result file records it). Absolute numbers carry that noise; write throughput differed by up to about 10% between runs of the same build and data |
| Date | 2026-10-14 |

### Fixture

Seeded through the API by `bench seed` with the defaults:

| | |
| --- | --- |
| Repositories | 4 (`acme/app-0..3`), each with `main` (200 files, a `.branvil/ci.toml` with one job), 500 PR branches and 400 spare branches (901 refs each, 3,604 in all) |
| Boards | 2 boards; each repository's tickets on one of them (two repositories per board), 6 labels per repository (seeded before tickets moved into repositories and repositories into workspaces: then 4 trackers, one per repository, held the tickets and the labels, and the boards were `acme/board-0..1`; `bench seed` now creates 2 workspaces `acme-0..1` with two repositories each, `acme-0/app-0..1` and `acme-1/app-2..3`, whose tickets are on their workspace's board, and gives the repositories the labels) |
| Members | 3 humans (one admin), 8 agents, 1 runner (seeded before Branvil had a single owner and fixed rights per member kind; `bench seed` now makes the admin the owner and the other two humans, `ken` and `mia`, agents, without grants) |
| Tickets | 10,000 (2,500 per repository): lanes new 25%, ready 6%, in progress 4%, done 65% (closed); agents assigned to all but 70% of the new ones; 0–3 labels |
| Pull requests | 2,000 (500 per repository), each linked to a ticket (`closes`), half with a review request; 1,402 closed afterwards (not merged: merges are fast-forward only), 598 open |
| Activity | 2,000 reviews, 1,000 inline threads with one reply each, 15,000 PR comments, 35,000 ticket comments (a third of them on a tenth of the tickets; 10% carry `meta`) |
| CI | 2,004 attempts (one per PR head and `main` tip), 80% success, 4 KiB log each |
| Size | `branvil.redb` 110.1 MiB (88.5 MiB allocated on disk), `repos/` 20.5 MiB, `ci/` 7.8 MiB |

The full fixture seeded in 11.3 minutes (fixed build below):

| Phase | Requests | Seconds | Requests/s |
| --- | ---: | ---: | ---: |
| members, repositories, trackers (now: members, workspaces, repositories) | 43 | 0.4 | 102 |
| Git push of 3,604 refs (4 pushes) | — | 14.7 | 245 refs/s |
| tickets (create on a lane, assign, close) | 24,860 | 145.5 | 171 |
| pull requests (create, review request) | 3,003 | 31.7 | 95 |
| CI (claim, log, finish; includes waiting for queue admission) | 6,227 | 137.3 | 45 |
| reviews, threads, comments | 54,000 | 339.9 | 159 |
| closing pull requests | 1,402 | 8.9 | 157 |

Seeding ran 8 requests at a time. Every write is one durable commit, so
seeding is bounded by the commit rate (below), not by the number of clients.
With the build before the fixes in [Fixes](#fixes-made-from-these-measurements),
the ticket phase ran at 121.5/s instead of 171/s (placing each ticket on the
`done` lane read the whole lane).

## Service: port operations

Each operation is what the GuildBotics port sends (`docs/port-mapping.md`),
timed from request to full response at the client:

| Operation | Requests |
| --- | --- |
| `cards` | candidates: `GET /workspaces/{w}/board/cards?lane=ready,in_progress&assignee=me&limit=100`, every page (one page here: ~60 cards per agent and board, with all their comments) |
| `patrol` | `GET /pulls?involves=me&state=open&limit=100`, every page (2–3 pages: 100–300 open PRs per agent, each with its activity) |
| `readiness` | `GET pulls/{n}`, `…/activity`, `…/checks`, `GET pulls/{n}` again |
| `ticket_read` | `GET tickets/{n}` and the first page of its comments |
| `comment_create` | `POST tickets/{n}/comments` (`_idem`: with an `Idempotency-Key`) |
| `label_update` | `PATCH tickets/{n}` adding one label and removing another |
| `pr_create` | `POST repos/{r}/pulls` on a branch without a PR, linking a ticket (`_idem`: with an `Idempotency-Key`) |
| `review_create` | `POST pulls/{n}/reviews` (`comment`) on the current head |
| `ticket_move` | `PUT tickets/{n}/board` to new / ready / in progress (end of lane) |
| `ticket_move_done` | the same into `done` (about 3,250 cards per board), then back; the time of the move into `done` only |
| `mixed` | per operation: 10% cards, 10% patrol, 20% readiness, 30% ticket read, 12% comment, 6% label, 6% move, 4% review, 2% PR create |

Results with the fixed build, cache 32 MiB. *Write wait* is the time a write
transaction waited for the single writer; *writers queued on arrival* is how
many write transactions were waiting or running when one arrived; *commit* is
redb's `commit()` (the durable sync). These three come from the service
(`GET /admin/diagnostics` `storage.queues`, histogram buckets about 19% wide,
so they are bucket bounds, not exact values).

| Operation | Conc. | Ops/s | p50 ms | p95 ms | max ms | Write wait p50 / p95 ms | Writers queued on arrival p50 / p95 / max | Commit p50 ms |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| `cards` | 1 | 226.2 | 3.5 | 4.1 | 66.3 | — | — | — |
| `cards` | 8 | 884.2 | 6.9 | 15.8 | 40.7 | — | — | — |
| `cards` | 32 | 1070.8 | 28.4 | 31.7 | 35.6 | — | — | — |
| `patrol` | 1 | 12.5 | 69.9 | 72.5 | 1411.4 | — | — | — |
| `patrol` | 8 | 65.4 | 89.4 | 121.5 | 805.7 | — | — | — |
| `patrol` | 32 | 89.6 | 321.8 | 445.0 | 526.6 | — | — | — |
| `readiness` | 1 | 30.9 | 32.4 | 33.7 | 37.1 | — | — | — |
| `readiness` | 8 | 186.2 | 42.2 | 47.7 | 64.4 | — | — | — |
| `readiness` | 32 | 201.8 | 156.9 | 166.0 | 175.1 | — | — | — |
| `ticket_read` | 1 | 2077.7 | 0.4 | 0.8 | 2.1 | — | — | — |
| `ticket_read` | 8 | 12414.6 | 0.6 | 0.9 | 1.1 | — | — | — |
| `ticket_read` | 32 | 17417.7 | 1.7 | 2.2 | 2.5 | — | — | — |
| `comment_create` | 1 | 131.0 | 7.4 | 9.4 | 15.6 | 0.0 / 0.0 | 0 / 0 / 1 | 7.2 |
| `comment_create` | 8 | 135.4 | 59.7 | 67.3 | 68.2 | 57.3 / 65.5 | 7 / 7 / 7 | 7.2 |
| `comment_create` | 32 | 139.0 | 234.4 | 244.2 | 247.8 | 229.4 / 262.1 | 31 / 31 / 31 | 7.2 |
| `comment_create_idem` | 1 | 80.3 | 12.1 | 14.3 | 23.7 | 0.0 / 0.0 | 0 / 0 / 1 | 6.1 |
| `comment_create_idem` | 8 | 80.8 | 99.7 | 111.5 | 115.4 | 49.2 / 57.3 | 7 / 7 / 7 | 6.1 |
| `comment_create_idem` | 32 | 78.8 | 398.8 | 444.9 | 446.8 | 196.6 / 229.4 | 31 / 39 / 39 | 6.1 |
| `label_update` | 1 | 147.1 | 6.7 | 8.5 | 10.2 | 0.0 / 0.0 | 0 / 0 / 0 | 6.1 |
| `label_update` | 8 | 156.1 | 51.0 | 58.9 | 63.2 | 49.2 / 57.3 | 7 / 7 / 7 | 6.1 |
| `label_update` | 32 | 157.7 | 201.0 | 223.0 | 224.2 | 196.6 / 229.4 | 31 / 31 / 31 | 6.1 |
| `pr_create` | 1 | 20.6 | 48.3 | 51.6 | 57.3 | 0.0 / 0.0 | 0 / 0 / 1 | 6.1 |
| `pr_create` | 8 | 80.4 | 82.9 | 186.1 | 269.1 | 5.1 / 14.3 | 1 / 3 / 7 | 5.1 |
| `pr_create` | 32 | 82.0 | 339.3 | 603.4 | 708.4 | 12.3 / 81.9 | 3 / 15 / 27 | 5.1 |
| `pr_create_idem` | 1 | 19.1 | 51.9 | 55.0 | 64.7 | 0.0 / 0.0 | 0 / 0 / 1 | 5.1 |
| `pr_create_idem` | 8 | 62.8 | 124.0 | 172.8 | 229.3 | 16.4 / 28.7 | 3 / 5 / 7 | 5.1 |
| `pr_create_idem` | 32 | 63.2 | 462.0 | 784.0 | 946.4 | 32.8 / 49.2 | 7 / 9 / 27 | 5.1 |
| `review_create` | 1 | 77.8 | 12.9 | 14.2 | 22.9 | 0.0 / 0.0 | 0 / 0 / 0 | 6.1 |
| `review_create` | 8 | 178.1 | 43.9 | 50.0 | 54.9 | 32.8 / 41.0 | 6 / 7 / 7 | 6.1 |
| `review_create` | 32 | 177.1 | 178.0 | 188.3 | 189.4 | 196.6 / 196.6 | 31 / 31 / 39 | 6.1 |
| `ticket_move` | 1 | 141.7 | 7.0 | 8.5 | 10.2 | 0.0 / 0.0 | 0 / 0 / 0 | 7.2 |
| `ticket_move` | 8 | 151.6 | 52.3 | 60.6 | 63.6 | 49.2 / 57.3 | 7 / 7 / 7 | 6.1 |
| `ticket_move` | 32 | 152.0 | 211.1 | 225.3 | 228.1 | 229.4 / 229.4 | 31 / 31 / 31 | 6.1 |
| `ticket_move_done` | 1 | 73.9 | 6.7 | 8.4 | 17.4 | 0.0 / 0.0 | 0 / 0 / 1 | 6.1 |
| `ticket_move_done` | 8 | 74.8 | 53.7 | 61.4 | 74.9 | 49.2 / 57.3 | 7 / 7 / 7 | 6.1 |
| `ticket_move_done` | 32 | 78.0 | 203.0 | 217.9 | 219.2 | 229.4 / 229.4 | 31 / 39 / 39 | 6.1 |
| `mixed` | 8 | 261.0 | 9.1 | 141.6 | 352.8 | 2.6 / 7.2 | 1 / 2 / 2 | 6.1 |
| `mixed` | 32 | 252.8 | 23.3 | 490.8 | 637.2 | 6.1 / 49.2 | 1 / 9 / 9 | 6.1 |

Requests inside the read operations (p50 / p95 ms):

- `cards` c=1: page 3.5 / 4.1 (n=200)
- `cards` c=8: page 6.9 / 15.8 (n=200)
- `cards` c=32: page 28.4 / 31.7 (n=200)
- `patrol` c=1: page 30.1 / 32.3 (n=600)
- `patrol` c=8: page 38.1 / 49.0 (n=545)
- `patrol` c=32: page 140.9 / 182.1 (n=543)
- `readiness` c=1: activity 6.6 / 7.4 (n=200); checks 12.4 / 13.5 (n=200); pull 6.5 / 7.1 (n=400)
- `readiness` c=8: activity 8.6 / 10.0 (n=200); checks 16.6 / 19.0 (n=200); pull 8.4 / 9.9 (n=400)
- `readiness` c=32: activity 31.2 / 34.7 (n=200); checks 62.2 / 68.4 (n=200); pull 31.9 / 34.4 (n=400)
- `ticket_read` c=1: comments 0.2 / 0.5 (n=200); ticket 0.2 / 0.4 (n=200)
- `ticket_read` c=8: comments 0.3 / 0.5 (n=200); ticket 0.3 / 0.4 (n=200)
- `ticket_read` c=32: comments 0.8 / 1.3 (n=200); ticket 0.8 / 1.1 (n=200)

What these numbers show (on this machine, with this fixture):

- **Writes are bounded by the durable commit.** A commit took 5–7 ms (p50),
  so one writer completes 130–180 writes per second whatever the
  concurrency; with *c* concurrent writers every writer finds the other *c*−1
  queued (31 at 32 clients), and latency grows linearly with *c* (≈ 7 ms ×
  *c*). An `Idempotency-Key` costs a second commit (the stored response), so
  keyed creates reach about half the rate (≈ 80/s).
- **Pull request paths are bounded by Git processes.** A PR read starts
  `git for-each-ref` (branch tips) and, once per range, `git diff` (changed
  files, cached in memory afterwards); checks add `git rev-list --count`, and
  while a job runs `git rev-list --first-parent` (its estimate). At 32
  clients these wait for the 8 Git command slots: `readiness` went from 32 ms
  (1 client) to 157 ms (32), and its requests from 6.5 ms to 32 ms.
  `pr_create` (48 ms alone) is mostly Git: the head is resolved and the
  changed files read before the transaction.
- **The first patrol after a start is slow** (max 1.4 s with one client; p95
  4.1 s at 32 clients on a cold service in a separate run): every open PR's
  changed-file count is computed once (one `git diff` each, 598 PRs). Later
  patrols use the cache.
- **Ticket and card reads are cheap:** 0.4 ms for a ticket and its comments
  with one client, 1.7 ms at 32 (17,000 operations/s); a candidates page of
  ~60 cards with their comments 3.5 ms.
- **Mixed load:** 250–260 operations/s at 8 and 32 clients; p50 9–23 ms, p95
  140–490 ms (the p95 is the patrol, PR create, and readiness share).

## Startup, memory, and files

Fixed build, the full fixture, cache 32 MiB:

| Measure | Value | How |
| --- | --- | --- |
| Startup (spawn → listening) | 389 ms the first start after copying the data, then 270–292 ms (5 starts) | `bench startup`; the OS file cache was not dropped (that needs root), so this is not a cold-disk start. It includes configuring every repository (2 Git commands each), the ref recovery scan, and opening redb |
| First `/health` after listening | 0.5–4.7 ms | |
| First candidates request after a start | 5.6–79 ms | |
| Resident memory, idle after start | 28.7–29.0 MiB | `ps` RSS 2 s after the first requests |
| Resident memory, peak during load | 97–120 MiB (patrol at 32 clients ~100 MiB, mixed ~120 MiB); stays there after the load ends | sampled every 100 ms and `/usr/bin/time -l` at exit; four repeated patrol runs at 32 clients stayed at 97–103 MiB and four mixed runs at 118–120 MiB, so it levels off rather than growing |
| Peak memory by cache size | 104 / 120 / 138 MiB with an 8 / 32 / 128 MiB cache | same full run each |
| `branvil.redb` | 110.1 MiB file, 88.5 MiB on disk (did not grow during the measured runs, ~6,000 extra writes) | |
| `repos/` | 20.5 MiB (3,604 refs, 4 repositories) | |
| `ci/` | 7.8 MiB (2,004 logs of 4 KiB) | |
| CPU during a full `bench run` | 139 s user+sys in 104 s wall, Git child processes included | `time -l` |

`time -l` counts Git processes the service waited for: its peak RSS is the
largest of the service and those children (the 100 ms samples of the service
alone agree with it here). Git child processes' own memory is not reported
separately.

### Cache size

The same full `bench run` with `storage.cache_size_mib` 8, 32, and 128
(p50 ms):

| Operation | 8 MiB | 32 MiB | 128 MiB |
| --- | ---: | ---: | ---: |
| `cards` (1 / 32 clients) | 3.7 / 29.2 | 3.5 / 28.4 | 3.5 / 32.1 |
| `patrol` (1 / 32) | 70.0 / 335.9 | 69.9 / 321.8 | 68.9 / 334.1 |
| `readiness` (1 / 32) | 32.6 / 155.2 | 32.4 / 156.9 | 32.3 / 161.0 |
| `ticket_read` (1 / 32) | 0.5 / 2.0 | 0.4 / 1.7 | 0.5 / 2.0 |
| `comment_create` (1 / 32) | 8.0 / 224.9 | 7.4 / 234.4 | 8.1 / 242.5 |
| `mixed` (8 / 32) | 9.1 / 19.8 | 9.1 / 23.3 | 6.6 / 20.3 |
| peak RSS | 104 MiB | 120 MiB | 138 MiB |

No latency difference beyond the run-to-run noise: with a 110 MiB file on an
otherwise idle disk, pages redb's cache does not hold come from the OS file
cache. The cache size shows up only in memory. A machine whose file cache is
under pressure, or a larger database, was not measured.

## Git over Smart HTTP

`bench git`, three repetitions each (ms), through the service's Smart HTTP
with Basic authentication (a member token), client and service on the same
machine:

| Repository | Size in Branvil | Clone | Fetch, nothing new | Fetch, 1 new branch | Push 1 commit, new branch | Push 1 commit, existing branch | First push into an empty repository |
| --- | ---: | --- | --- | --- | --- | --- | --- |
| small: 10 commits, 20 files | 0.2 MiB | 113, 113, 113 | 71, 78, 78 | 114, 113 | 152, 160, 159 | 161, 164, 162 | 983 |
| medium: 3,000 commits, 5,000 files (generated text) | 22.8 MiB | 1231, 1245, 1297 | 78, 77, 81 | 119, 122 | 173, 186, 173 | 169, 175, 162 | 1135 |
| fixture `acme/app-0`: 901 branches | 5.2 MiB | 212, 210, 209 | 104, 89, 89 | 126, 116 | 175, 178, 170 | 171, 173, 173 | — |

A push of one commit takes 40–90 ms longer than fetching it: the pre-receive hook calls back
into the service (permission check), and the push is recorded (ref snapshot,
CI queue) before `git push` returns (these numbers also include the external
sync queue Branvil had then). The service's peak RSS during
these transfers was 83 MiB (Git child processes included, see above).

## GUI

The bundle (`web/dist`, Vite production build): `index.html` 0.4 kB, CSS 6.4
kB (2.0 kB gzip), JavaScript 431 kB (129.2 kB gzip). The service sends the
files uncompressed (`Cache-Control: public, max-age=3600`).

Time to a rendered board, `web/e2e/perf.spec.ts` in Google Chrome
(`BRANVIL_E2E_CHANNEL=chrome`, a new browser context per run, Playwright
tracing off), board `acme/board-0` of the fixture (now the board of workspace
`acme-0`, `BRANVIL_E2E_WORKSPACE`): 1,695 open cards (the
board's default view), fetched in 17 pages. The spec enters the GUI with a
`branvil open --print` link and counts from the board page's own navigation.
Besides the first painted frame it reports *busy until*: a timer on the page
notes every stretch over 50 ms in which it could not run, and the run waits
until there was none for 2 s, so work the browser does after that frame
counts too (the page answers no input while it lasts).

Measured on 2026-10-15, release builds of the same service, the fixture
seeded again with `bench seed` defaults and an APFS clone of it per series;
two series of 5 runs each per build, alternating builds; load average 5–6
(other work on the machine). The first run of a series meets a service that
has not served the board yet (card pages end at 253–267 ms instead of
75–114 ms):

| Measure | Before (10 runs) | After (10 runs) |
| --- | --- | --- |
| All card pages fetched | 78–90 ms after navigation (first runs 253–267 ms), 2.1 MB of JSON | the same: 75–95 ms (255–257 ms) |
| Cards in the DOM | 753–764 ms (first runs 934–953 ms) | 185–206 ms (363–370 ms) |
| First frame painted with the cards | 6.10–6.20 s (first runs 1.41–1.43 s, see below) | 447–470 ms (610–623 ms) |
| Busy until | 6.11–6.44 s | 435–458 ms (610–623 ms) |
| Main thread blocked (stretches over 50 ms) | 6.04–6.19 s | 355–376 ms |
| DOM nodes | 66,099 | 37,284 |
| JS heap | 30.9–31.8 MB | 21.6–22.6 MB |
| Checks requests (the board loads up to 30) | answered by 0.84–1.06 s | answered by 0.29–0.49 s |

Interaction on the same board after it settled (a Playwright script, not part
of the repository; 3 runs per build): moving one card with its lane menu
(optimistic update, the `PUT`, and the refetch of the 17 card pages) kept the
main thread in tasks over 50 ms for 433–443 ms before and 60–62 ms after; a
drag passing over 40 cards (one synthetic `dragover` per card) took 4.0–4.1 s
(about 100 ms per event) before and 0.46–0.52 s (about 12 ms) after.

What took the time (a Chrome trace and a CPU profile of one load of the
before build, taken through Playwright with `browser.startTracing` and the
DevTools protocol's profiler):

- Fetching and rendering were not the problem: the 17 pages arrived within
  0.1 s and React inserted every card in one commit (0.75 s of script).
- After that commit the main thread ran one task of 5.4 s with no script on
  its stack: 195 rounds of style recalculation and a layout of the whole
  document (about 55,000 layout objects, ~6.6 ms each round), the browser's
  own work for the form every card carried to assign a member (an input with
  a `<datalist>` of the 11 member names). Each first run of a series painted
  at 1.4 s only because this task came after the first frame; the page stayed
  blocked until 6.4 s all the same.
- Two experiments on a debug service (2 runs each): without the datalist but
  with the form, the frame came at 1.1 s and a 3.0 s layout task followed it;
  with the form rendered only while a card's "Assign" is open, the frame came
  at 0.76 s and nothing followed.
- The suspected cause, re-rendering the whole board as each check answers,
  was not it: each badge has its own query, so an answer re-renders only that
  badge (after the fix no task over 50 ms follows the 30 answers). Re-rendering every card did
  happen on every change of the board's own state: each `dragover` of a drag
  re-rendered all 1,695 cards (about 100 ms each).

What changed:

- A card renders its assign form, input, and datalist only while its
  "Assign" is open (DOM nodes 66,099 → 37,284).
- Cards are memoised components with stable props (the move callback reads
  the current cards when called, member names and positions are memoised),
  and a `dragover` over the same place sets no new state, so a drag, a filter,
  or a check answer re-renders no card (`web/src/__tests__/board.test.tsx`
  counts the renders).
- Not adopted: showing only the first part of a long lane behind a "show
  more" control. With the two changes above the whole board of 1,695 cards is
  painted and idle within 0.45–0.62 s, so a lane limit would add state to
  moving, dragging, and filtering without a measured need. Also not adopted
  (earlier): `content-visibility: auto` on cards.

Not measured: other browsers, boards with closed cards included, real
(non-synthetic) drag and drop.

Note: with Playwright's trace recorder on (`trace: "retain-on-failure"` in
`playwright.config.ts`), each locator step snapshots the DOM, which took
over 20 s per step on this board; timing runs need `--trace off`.

## Fixes made from these measurements

Measured on fixture A (same shape, seeded by the build before the fixes; its CI
queue was left partly waiting), both builds on copies of the same data,
cache 32 MiB (ops/s, p50 / p95 ms):

| Operation | Conc. | Before: ops/s, p50 / p95 ms | After: ops/s, p50 / p95 ms |
| --- | ---: | --- | --- |
| `cards` | 1 | 119.4, 7.2 / 7.7 | 235.4, 3.5 / 3.9 |
| `cards` | 8 | 548.9, 12.1 / 20.9 | 857.6, 7.3 / 17.6 |
| `cards` | 32 | 603.1, 48.6 / 65.7 | 1103.0, 27.2 / 31.0 |
| `ticket_move` | 1 | 105.7, 8.1 / 13.4 | 140.7, 7.0 / 8.9 |
| `ticket_move` | 8 | 112.2, 70.9 / 82.0 | 145.3, 55.6 / 62.8 |
| `ticket_move` | 32 | 112.5, 283.9 / 304.8 | 158.7, 193.2 / 224.1 |
| `ticket_move_done` | 1 | 28.0, 24.3 / 25.4 | 76.3, 6.0 / 10.8 |
| `ticket_move_done` | 8 | 28.6, 146.2 / 197.9 | 80.0, 47.1 / 64.6 |
| `ticket_move_done` | 32 | 28.5, 535.0 / 759.5 | 76.0, 189.0 / 252.1 |
| `pr_create_idem` | 1 | 18.9, 52.7 / 55.1 | 17.7, 53.9 / 67.3 |
| `pr_create_idem` | 8 | 18.9, 422.9 / 436.0 | 48.2, 154.9 / 257.0 |
| `pr_create_idem` | 32 | 19.1, 1680.5 / 1694.3 | 61.8, 493.0 / 803.1 |
| `comment_create_idem` | 1 | 80.5, 12.0 / 14.3 | 79.8, 12.7 / 14.1 |
| `comment_create_idem` | 8 | 83.3, 95.9 / 103.6 | 82.5, 97.8 / 105.1 |
| `comment_create_idem` | 32 | 83.0, 386.7 / 401.1 | 79.9, 403.9 / 410.0 |
| `pr_create` | 1 | 20.8, 48.0 / 49.9 | 20.6, 48.3 / 51.2 |
| `pr_create` | 8 | 78.6, 85.1 / 183.6 | 80.4, 85.3 / 180.7 |
| `pr_create` | 32 | 82.1, 343.7 / 586.7 | 82.6, 341.1 / 593.0 |
| `comment_create` | 1 | 144.6, 7.0 / 8.4 | 129.0, 7.7 / 9.2 |
| `comment_create` | 8 | 144.3, 55.6 / 61.1 | 129.6, 62.9 / 68.5 |
| `comment_create` | 32 | 145.3, 222.5 / 245.0 | 132.7, 241.2 / 256.8 |

- **Card placement read the whole target lane** inside the write transaction
  (every card and its ticket record) to find the last rank or a neighbour; the
  `done` lane keeps every finished ticket, so creating, moving, or
  closing-by-merge into it grew with history while holding the single writer.
  It now reads at most two index entries. `ticket_move_done`: 24.3 → 6.0 ms
  with one client, 28 → 76 operations/s.
- **Cards with a lane filter scanned the board's whole card index** to the
  end (the `done` lane included). Only the requested lanes are read now:
  `cards` 7.2 → 3.5 ms with one client, 603 → 1,103 operations/s at 32.
- **`Idempotency-Key` requests ran one at a time service-wide** (one mutex
  held across the whole request: Git reads and two commits). They now wait
  only for the same member and key: keyed `pr_create` at 32 clients 19 → 62
  operations/s, p50 1,680 → 493 ms. Keyed comments are commit-bound either way
  (two commits each).
- **A token's `last_used_at` write** (once a minute per token) made that
  read request wait behind the writer queue; it now runs in the background
  (`cards` at 8 clients: max 62 → 38 ms; a small effect at these rates).

Rows without a fix (`pr_create`, `comment_create`) show the noise between the
two runs (up to 10% on commit-bound writes).

## Totals

What one machine running this fixture carried, from the sections above:

| Part | Memory | Other |
| --- | --- | --- |
| `branvil serve` | 29 MiB idle; 97–120 MiB peak under 32 concurrent clients | 130–180 durable writes/s; CPU 139 s for a 104 s full run, Git children included |
| Git (service side) | child processes per request, not measured separately | 8 command slots; PR reads 6–12 ms each, 30–60 ms at 32 clients |
| Data on disk | 110 MiB redb + 20.5 MiB repositories + 7.8 MiB CI files | |
| GUI (browser) | 22–23 MB JS heap for a 1,695-card board | 0.45–0.62 s to the first painted, idle board of that size |
| `branvil runner` | 20 MiB (debug build, [CI executor](#ci-executor)) | |
| One running CI job | ~170 MiB (VM process + `msb run`) for a 512 MiB guest running a shell | 0.24–0.33 s sandbox startup |

So with one CI job running, the executor needs more memory than the service
under load; each further concurrent job adds about 170 MiB plus what the job
itself touches (up to the memory every job gets, `ci.executor.max_memory_mib`).

## Not measured

- A cold-disk start (dropping the OS file cache needs root), Linux and Windows
  hosts, other storage.
- Fixtures larger than the default and longer histories (placement no longer
  depends on the size of the `done` lane, but the GUI's board view and the
  closed-item list were not timed at larger sizes; the closed-item list now
  reads the repository records to find the workspace's repositories, and was
  not measured since).
- Merges (seeded PRs are closed, not merged), polling and publishing to an
  upstream host, CI
  with a real executor under API load, the event stream with many GUI clients.
- Git transfers of large repositories (hundreds of MiB) or many concurrent
  transfers.

## CI executor

What one job costs on the runner's machine: microsandbox microVMs started by
`branvil runner` through `msb run` (msb 0.6.17).

Conditions:

| | |
| --- | --- |
| Machine | Apple M1 Pro, 16 GiB, macOS 26.6.2 (build 25G83) |
| Build | `branvil` debug build (`cargo test`); `msb` is the GuildBotics-deployed release binary |
| Image | `docker.io/library/alpine@sha256:5291449c…8fa8` (3.9 MiB, already in Branvil's image cache) |
| Job | 1 vCPU, 512 MiB, network off, the commit's tree (two files) mounted at `/work`, run as root with `/tmp` in memory |
| Harness | `BRANVIL_REAL_EXECUTOR=1 cargo test --test main real_executor:: -- --nocapture` (three jobs of one run, one after another: a probe job, an `exit 3` job, a `sleep` job stopped by a 5 s timeout) |
| Sampling | `ps -o rss` every 250 ms while the jobs run (peaks can be missed between samples) |

Results of seven runs on 2026-10-14:

| Measure | Values | Notes |
| --- | --- | --- |
| Sandbox startup (spawn of `msb run` → first output of the job script) | 236, 254, 256, 256, 276, 301, 332 ms | logged by the runner as `[branvil] sandbox started in … ms`. **Correction:** `msb run` hands a command's output over only when it exits, so these runs measured spawn → the end of the probe job (whose commands take a few ms): an upper bound of the startup |
| Sandbox startup, since the runner reads the job's log file (`docs/ci.md`) | 251, 251, 302 ms | spawn → the script's first line, read every 50 ms (so up to 50 ms late) |
| Peak RSS of the VM process (`msb sandbox`) | 119.0–120.0 MiB (121 856–122 912 KiB) | guest memory is 512 MiB; RSS is what the host committed |
| Peak RSS of the attached `msb run` process | 43.6–49.2 MiB (44 688–50 384 KiB) | one per running job |
| Peak RSS of `branvil runner` (debug build) | 20.1–20.3 MiB (20 576–20 752 KiB) | while it serves the jobs |
| Three jobs end to end (runner process start → all three completed) | 6.0–6.5 s | includes the 5 s timeout of the third job |

So one running job costs about 170 MiB of host memory (VM process plus its
`msb run` parent) for a 512 MiB guest running a shell, on top of the runner's
20 MiB. A job that touches more guest memory raises the VM figure up to the memory
every job gets (`ci.executor.max_memory_mib`). These runs predate jobs getting a
Git checkout at `/work`, a non-root user, and `/tmp` on a disk volume of their
own (`docs/ci.md`, "The job's environment"), and the harness's fourth job (a
Git image, run twice) that came with them; they were not measured again. Not
measured yet: a release build of the runner, other images, Linux and Windows
hosts, and the service's own CPU and memory during CI.

### Branvil's own pipeline

The six jobs of `.branvil/ci.toml` on the same machine, read from the lines
the runner writes in each job's log (`docs/ci.md`, "The runner"), on
2026-10-23: a throwaway service and one runner (debug build, `max_cpus` 4,
`max_memory_mib` 6144, one job at a time), the image
`guildbotics/agent-environment:dev`, the repository's network allowed. **Cold**
is `fe6defc` on the default branch with an empty cache; **warm** is the next
commit, `8a40595`, as the head of a pull request, restoring the protected
branches' entries the cold run saved.

| Job | Commands, cold | Commands, warm | Cache restore, warm | Cache save, warm | Peak memory (cold / warm) |
| --- | --- | --- | --- | --- | --- |
| fmt | 0.7 s | 0.7 s | — | — | 557 / 560 MiB |
| clippy | 67.0 s | 8.2 s | 2.4 s (485 MB) | 3.1 s | 2111 / 1813 MiB |
| test | 162.7 s | 93.9 s | 2.3 s (999 MB) | 4.3 s | 2695 / 3104 MiB |
| web | 71.7 s | 58.5 s | 0.2 s (48 MB) | 1.9 s | 1699 / 2138 MiB |
| dev-script | 332.0 s | 333.9 s | — | — | 599 / 596 MiB |
| branvil-integration | 168.5 s | 58.7 s | 5.2 s (1228 MB) | 8.8 s | 2196 / 2004 MiB |

These runs predate the rule that only a protected branch's run saves the
cache (`docs/ci.md`, "Dependency cache"): a pull request's run, like the warm
one, now restores the cache and saves nothing.

Every job: the source fetched in 16–46 ms (a bundle of 0.9 MB), checked out
in 76–127 ms, its sandbox started in 252–553 ms and removed in 40–66 ms.

- One commit takes 821 s cold and 588 s warm from the first job's start to
  the last one's end, the jobs one after another. `dev-script` is more than
  half of the warm run, and its time is the tests' own (`Ran 76 tests in
  333.686s`).
- What the runner adds (source, checkout, sandbox start and removal, cache
  restore and save) is about 30 s of the warm run (5 %). In the warm `test`,
  Cargo compiles the branvil crate for 53.5 s of the 93.9 s (the pipeline
  rebuilds it on purpose, `.branvil/ci.toml`).
- A job's peak memory is what the guest touched: about 560 MiB for a VM that
  only boots and runs a shell command, and up to about 3.1 GiB for `test`,
  below the 6 GiB every job gets.

Incremental compilation, on the same conditions on 2026-10-23 (`1c4fc75`):
each pair is a commit and the next one, a small change of `src/util.rs`
(a new function), the second restoring the first's cache, with the
pipeline's `CARGO_INCREMENTAL=0` and without it.

| Job (the second commit) | `CARGO_INCREMENTAL=0` | Incremental | Its cache entry |
| --- | --- | --- | --- |
| `test`: Cargo's `Finished … in` | 54.1 s | 42.5 s | 956 → 2218 MiB |
| `test`: commands | 98 s | 87 s | |
| `branvil-integration`: commands | 49 s | 44 s | 1172 → 1689 MiB |
| `clippy`: entry | | | 462 → 644 MiB |

Incremental data saves about 17 s of compiling a commit (`clippy` was not
compared: the change failed its lint `items_after_test_module`) and more than
doubles the largest entries, which the cache copies twice per job (2.6 s and
5.6 s for `test`'s 2218 MiB). The pipeline keeps `CARGO_INCREMENTAL=0`.

### Web tests

`cd web && npm run test` records file and test durations in
`web/test-results/timing.json`. The `web` CI job uploads that file as the
`web-tests` artifact, including on test failure, and streams Vitest output
into the job log. To keep a
local measurement outside the checkout:

```sh
cd web
BRANVIL_TEST_RESULTS=/tmp/branvil-web-timing npm run test
```

Initial measurements for #78 on 2026-10-24: 231 tests in 20 files, Vitest 5.0.2,
jsdom 30.1.1, Node.js 24.22.1, at most four workers. The host is an Apple
M1 Pro (10 cores, 16 GiB), macOS 26.7.1. Production CI uses a Linux 6.12.99
microVM with four CPUs and 6144 MiB per job, image configuration
`sha256:67d06b064a73876d72ccf609b0b9854cb5f19b19cfc6ea9dd8f39b8b1d067932`,
and a runner configured for two simultaneous jobs. Each row below is one
run; the host also runs the service and other desktop applications.

| Run | Commit | Vitest wall time | Result |
| --- | --- | ---: | --- |
| Before, normal pipeline; `test` observed running alongside `web` | `4cb2ffe` | 71.79 s | 231 passed |
| Before, web-only pipeline | `652e221` | 65.53 s | 231 passed |
| After, web-only pipeline | `7f2a712` | 36.84 s | 231 passed |
| After, normal pipeline; Rust `test` running alongside `web` | `9f0c6e0` | 32.36 s | 231 passed |
| After, normal pipeline repeated; Rust `test` running alongside `web` | `ec3dde1` | 77.88 s | 231 passed |

The service's attempt-start timestamps in `~/Library/Logs/branvil.log` and
the completed job logs in `<data>/ci/logs/` were checked across repositories.
Completed logs' last-write times bound their ends; service restarts bound
older interrupted logs. The test intervals in `timing.json` were
12:57:41–12:58:47 and 13:00:21–13:00:58 JST for the two web-only runs:
no other job overlapped either interval. In all three normal-pipeline runs,
the Rust `test` job overlapped the web tests. The runner's two-job limit
was unchanged throughout.

Before the change, the shared run was 6.26 s slower than the solitary run;
after it, the first shared run was 4.48 s faster, but its repeat took
77.88 s. These are observations, not
isolated estimates of the cost of another job: the individual phases and
background activity also vary between runs.

Vitest's `Duration` breakdown, as percentages of all tracked phases (summed
across workers, not percentages of wall time):

| Run | Tests | Environment | Import | Setup | Transform | Worker |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Before, normal pipeline | 40% | 29% | 19% | 8% | 3% | <1% |
| Before, web-only pipeline | 52% | 23% | 18% | 5% | 2% | <1% |
| After, web-only pipeline | 52% | 13% | 26% | 6% | 1% | 3% |
| After, normal pipeline | 72% | 10% | 11% | 4% | 1% | 2% |
| After, normal pipeline repeated | 74% | 10% | 10% | 3% | 1% | 3% |

The normal pipeline spent 78.33 s in total creating jsdom environments,
once per file. File test durations were `relations` 25.68 s, `pull` 22.28 s,
`board` 13.89 s, `ci` 12.05 s, `settings` 11.87 s and `pages` 5.77 s. The
slowest individual tests were closed-ticket read retries (3.25 s), runner
polling (3.13 s), image-operation polling (3.09 s) and board filtering
(2.73 s).

The two web-only runs used the same image, dependencies and worker limit.
Wall time fell by 28.69 s (44%); cumulative environment setup fell from
57.65 s to 17.51 s (70%). The slowest test after the change took 1.42 s.
The first normal-pipeline run took 32.36 s (55% less than the baseline),
but a repeat took 77.88 s, so the shared-run wall-time improvement was not
consistent. Cumulative environment setup fell from 78.33 s to 12.14 s in
the first run and 29.74 s in the repeat. Test execution dominated the
repeat (74% of tracked time); its slowest test was board filtering at
4.78 s. Environment reuse does not eliminate contention during rendering.

The changes reduce two measured costs:

- `pool: "vmThreads"` loads jsdom once per worker while giving each file a
  fresh VM context and window ([Vitest's pool documentation](https://vitest.dev/config/pool)).
  File isolation stays enabled. The default
  pool had started a new process and loaded jsdom for every file.
- The two polling tests advance only the interval clock by 2 s or 3 s;
  rendering, user actions and query notifications keep their real clocks.
  The test renderer sets query retry delays to zero while keeping the
  production retry policy. The closed-ticket failure test checks all three
  attempts and the fourth, successful read after Retry. No application
  polling or retry setting changes.

Locally, the original suite took 14.27–18.17 s in four runs; the updated
suite took 11.23–25.54 s in three runs. Background activity varied between these local runs,
so the production measurements above are the comparison to use. The
updated suite also passed with shuffled execution (seed 78, 15.10 s). Disabling
polling deliberately failed both polling tests; disabling retries failed
the closed-ticket test. A heap measurement of the VM pool reached 342 MiB
per worker; this is JS heap, not total process or microVM memory.

The timed runs above used Vitest's 5 s test timeout. Although all passed,
the repeated shared run reached 4.78 s (96% of that limit), so only after
reducing the measured costs was `testTimeout` raised to 10 s, about twice
that maximum. These whole-test timings do not establish headroom for
Testing Library's separate query deadline; the per-query measurement below
sets that deadline. The normal-pipeline verification is the `web` job
of [PR !38](http://127.0.0.1:7878/GuildBotics/Branvil/-/pulls/38), whose
`web-tests` artifact records the same measurements for the current head.
The historical failures in #78 reached 5.58–6.48 s per test and missed
the 1 s query deadline. The rows above report the observed runs, not a
guarantee about all future host loads.

Selected file and individual-test durations from the web-only comparison
(execution only, excluding each file's environment and import):

| File or test | Before | After |
| --- | ---: | ---: |
| `relations.test.tsx` | 30.27 s | 15.77 s |
| `ci.test.tsx` | 15.55 s | 6.13 s |
| `board.test.tsx` | 13.65 s | 7.80 s |
| `pages.test.tsx` | 7.55 s | 4.21 s |
| Image-operation polling test | 3.32 s | 0.90 s |
| Runner polling test | 3.13 s | 0.19 s |
| Closed-ticket retries test | 3.43 s | 0.31 s |

#### Query wait headroom

After taking the drawer change `36bc560` from main, the suite had 233 tests.
A temporary wrapper preserved RTL's existing `getConfig().asyncWrapper`,
recording each call's elapsed time and stack in test metadata. Its clock
was captured before tests installed fake timers: advancing the test clock
by 30 s moved the captured clock by 0.3 ms; replacing it with a dynamic
global clock made that check fail. Call stacks distinguish `waitFor` and
`findBy*` from user-event actions, which use the same wrapper. The measured
boundary includes RTL's final event-loop drain, so it is a conservative
envelope around the actual query wait.

The first production run, [`55e0220`](http://127.0.0.1:7878/GuildBotics/Branvil/-/jobs/att_01M5P3RW4JFQH6C62B4KKQBE88), retained the 1 s query deadline and
10 s test deadline: 12 tests failed (nine query deadlines and three test
deadlines). Of 495 recorded query waits, 28 reached at least 1 s. Failed
calls included diagnostic generation, so their longest values
were not used to choose the new deadline.

The next run, [`91c383c`](http://127.0.0.1:7878/GuildBotics/Branvil/-/jobs/att_01M5P4AMM3BQ5JKTZ06N0ENX7P), temporarily allowed 5 s per query and 30 s per test
to measure successful waits without the 1 s cutoff. All 233 tests passed.
The same four-CPU, 6144 MiB production job overlapped main's
`branvil-integration` job for the entire 44.67 s test interval
(13:59:09–13:59:54 JST, verified from runner logs). Of 839 wrapper calls,
521 were query waits, 318 were user-event actions and none were unclassified.
All query waits succeeded; four exceeded the old 1 s deadline.

| Query wait envelope | Milliseconds |
| --- | ---: |
| Median | 33.12 |
| p95 | 464.52 |
| p99 | 874.75 |
| Maximum | 1499.77 |

The maximum was 150% of the old query deadline. `asyncUtilTimeout` is now
3000 ms, about twice that conservative maximum (50% utilisation). The
longest complete test in this run was 3.29 s; `testTimeout` remains 10 s.
The temporary wrapper, metadata traces, 5 s / 30 s measurement limits and
two-job measurement pipeline were removed; the normal six jobs run again.

A regression test delays the warnings response beyond the former 1 s
deadline and waits through the ordinary `findByRole` default. Changing the
query deadline back to 1 s makes it fail. This brings the retained suite to
234 tests. Log streaming was checked with a
command that emits stdout/stderr and then blocks: the output arrives before
exit, survives termination and preserves a failing exit status. Restoring
the buffered command makes this check fail. The production run also showed
Vitest output in the job log before the job ended. Only `timing.json` is
retained as an artifact; diagnostics continue to stream into the job log.
