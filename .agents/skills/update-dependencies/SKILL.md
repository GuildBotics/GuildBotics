---
name: update-dependencies
description: Update GuildBotics' dependencies unattended - raise the pinned AI CLI tools and the Node.js they run on to the latest stable releases and resolve the repository's open dependency alerts - and create a PR, without real-device validation.
---

# Updating the dependencies: AI CLI tools, their Node.js, and dependency alerts

Invoke with `$update-dependencies` or "依存を更新して" for both parts, or name
one part ("AI CLIツールを更新して", "依存アラートを解消して") to do only that. Check the official releases and the open
dependency alerts, update the pins and the locks, run the non-device quality
checks, and create a PR without asking for confirmation. Invoking this
update workflow includes committing and pushing its branch and creating or
updating its PR. Do not merge it; real-device checks, if any, happen after the
merge.

GuildBotics pins its provisioned tools in `CliAgentProvision` in
`guildbotics/intelligences/cli_agents.py`, and the Node.js that Codex, Claude
Code and Copilot run on in `NODE_VERSION` and `NODE_SHA256` in
`guildbotics/environment/snapshot.py`; the snapshot installs that Node.js over
the base image's. Its own libraries are locked in `uv.lock` (exported to
`guildbotics/environment/requirements.txt` for the snapshot),
`desktop/package-lock.json`, and `desktop/src-tauri/Cargo.lock`, and the code
host reports the vulnerable ones as dependency alerts. This workflow updates
the pins and the locks; it does not establish that the new releases work on a
real device.

## Scope

- **By default, do both parts: raise every tool and Node.js to its latest
  release, and resolve every open dependency alert.** Do only what the user
  names when they name a part (some tools, Node.js, or the dependency
  alerts). "Latest" is what the tool's own default install gives: the npm
  `latest` dist-tag, Grok Build's `stable` channel, the Antigravity manifest.
  Never a prerelease, alpha, or `next` tag. For Node.js it is the newest
  release of the pinned major; moving to another major is a human decision.
- Fetch current versions and alerts on every run. Decide what is needed
  against the remote default branch, which is what ships: a pin or a fix that
  only the update branch has still needs this run's PR. Finish without file
  changes or a PR only when the default branch already has the latest
  releases and every fix that can be made.
- One PR for the whole update, separate from any other change. Use the
  fixed head branch `codex/update-dependencies` and the remote default branch
  as base. Reuse an open PR with that head and base, preserving human
  verification commits and others' changes. Do not infer ownership from titles
  or dates, or create a duplicate PR.
- A tool whose release cannot be retrieved, or whose update cannot pass the
  non-device checks, stays on its current pin; an alert that cannot be
  resolved ([section 3](#3-resolve-the-dependency-alerts)) stays open. Explain
  each and update the others. Unperformed real-device checks do not prevent an
  update or PR.
- Do not build snapshots, start microVMs, run provider CLIs or real-device
  contract/smoke tests, or use saved provider logins. Do not request approval
  or wait for someone to perform these checks.
- If a human later requests real-device checks on the update PR, use the
  [verify-ai-cli-tools skill](../verify-ai-cli-tools/SKILL.md); do not invoke
  it as part of this unattended workflow.
- Not touched: the Python and TypeScript sources (`guildbotics/`,
  `desktop/src`) beyond what
  [section 2](#2-update-what-depends-on-the-version) allows for a tool
  (`guildbotics/environment/requirements.txt` is not a source but the lock's
  export), and the base images (`IMAGE` in `guildbotics/environment/image.py` and the
  Dockerfile's `FROM`). The tools and their Node.js are installed inside the
  snapshot by npm or an install script, never through the repository's
  dependency management or a base image; the libraries change only through
  their manifests and locks.

## 1. Isolate the update and find the latest versions

Work in a dedicated worktree or checkout on `codex/update-dependencies`.
For an open update PR, continue its branch, first merging the current remote
default branch into it, so that what it compares and changes starts from
today's default branch (a fix merged there since makes an alert nothing to do
instead of a conflict); if no open PR exists, base the new update on the
current remote default branch. A branch left by a PR closed without a merge is
continued the same way when it holds only earlier runs of this workflow; a new
PR then carries its changes (section 5). When it holds anything else, stop and
report it (the member cannot force-push to replace it). Keep a checkout served by Desktop or `guildbotics start`
unchanged: those processes can rebuild snapshots from its pins after restart.

| Tool | Latest | Pin in `cli_agents.py` |
| --- | --- | --- |
| Codex | `npm view @openai/codex dist-tags.latest` | `package="@openai/codex@<v>"` |
| Claude Code | `npm view @anthropic-ai/claude-code dist-tags.latest` | `package="@anthropic-ai/claude-code@<v>"` |
| GitHub Copilot | `npm view @github/copilot dist-tags.latest` | `package="@github/copilot@<v>"` |
| Grok Build | `curl -fsSL https://x.ai/cli/stable` | `bash -s <v>` in `install` |
| Antigravity | the two manifests below | versioned URL and both `sha512` in `install` |
| Node.js | the release index below | `NODE_VERSION` and both `NODE_SHA256` in `snapshot.py` |

Node.js lists its releases newest first. Take the first one of the pinned
major `<m>`, then the digests of its two Linux archives: `linux-x64`'s goes
to `x86_64`, `linux-arm64`'s to `aarch64`.

```bash
curl -fsSL https://nodejs.org/dist/index.json \
  | python3 -c 'import json, sys; print(next(r["version"][1:] for r in json.load(sys.stdin) if r["version"].startswith(f"v{sys.argv[1]}.")))' <m>
curl -fsSL "https://nodejs.org/dist/v<v>/SHASUMS256.txt" | grep -E 'node-v<v>-linux-(x64|arm64)\.tar\.gz$'
```

Grok Build's installer (`https://x.ai/cli/install.sh`) reads the same
`stable` pointer when no version is given.

Antigravity's installer (`https://antigravity.google/cli/install.sh`) reads a
manifest per platform. Take `version`, `url` and `sha512` from both:

```bash
for platform in linux_amd64 linux_arm64; do
  curl -fsSL "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/$platform.json"
done
```

The `url` carries a build ID (`<version>-<build id>/`). Put that directory into
the pinned URL, `linux_amd64`'s `sha512` on the `x86_64)` line and
`linux_arm64`'s on the `aarch64)` line. If the manifests are gone, read the
current `DOWNLOAD_BASE_URL` from the installer.

## 2. Update what depends on the version

Find every mention with these two searches. Together they cover the full
version, the minor-only form (`Codex 0.153`), descriptions measured on a past
version, and the documented baselines:

```bash
# A tool name next to a version, anywhere in the repository
git grep -nIiE '(codex|claude[- ]code|grok( build)?|copilot( cli)?|antigravity( cli)?|agy)[^0-9(=_]{0,24}[ @/]v?[0-9]+\.[0-9]+(\.[0-9]+)?([^0-9a-z]|$)' \
  -- . ':!tests/guildbotics/environment/test_snapshot.py' ':!.agents/skills/update-dependencies'
# Every full version, and every version-named fixture, in the files that describe the tools
git grep -nIE '(^|[^0-9.])[0-9]+\.[0-9]+\.[0-9]+([^0-9.]|$)|_[0-9]+_[0-9]+_[0-9]+\.json' \
  -- guildbotics/intelligences/cli_agents.py guildbotics/guest \
     guildbotics/templates/intelligences/cli_agents tests/guildbotics/guest \
     'docs/native_agent_runtime.*.md' | grep -v 'node:'
```

`test_snapshot.py` uses `9.9.9` as a made-up pin, the `node:` lines name a
base image, and this skill quotes versions as examples; nothing else they print
is noise.

Treat each hit by what it claims:

- **The pin, or a claim about the pinned version** ("pins", "固定しており"):
  set it to the new pin. If the same sentence claims it was verified, split
  the pin from the historical verification and keep that verification on
  the version actually tested.
- **An observation of one version** ("measured / observed on", "実測", a
  version-named fixture, "Verified against", "verified baselines",
  "動作確認済みの基準バージョン", a comment quoting a tool's output): leave
  it on the observed version. Do not rename fixtures or advance a measured
  range to an untested release.
- **A version requirement of the tool** ("before 2.1.246", "1.1.11 以降", "the
  1.0.83 server had no `account.getQuota`"): leave it.
- **Never leave a description measured on a version newer than the pin.** The
  product runs the pin, so such a description says nothing about it. Raising
  every tool to its latest normally removes the case. If a tool must stay on
  an older pin, qualify the observation as outside that pin's verified behavior.

Also check by hand:

- The limitations `docs/native_agent_runtime.*.md` attributes to a tool's
  version (no `usage_update`, no context size, failures reported as prose, an
  unverified capability): read the official release notes for relevant
  changes. Attribute new behavior to those notes; keep the previous measured
  limitation and make clear that its behavior on the new pin is unverified.
- `guildbotics/templates/intelligences/cli_agents/*/default.yml`: choices read
  from a tool's own answer (Copilot's effort levels, from `session/new`) stay
  unchanged unless official release information establishes a necessary change.
- Codex: `docker/agent-environment/Dockerfile` removes the image's bubblewrap
  because Debian's 0.8.0 could not exec Codex's helper, and
  `guest/codex.py` says so. Preserve this configuration; this workflow
  does not test the new Codex release's bundled helper.

Make adapter or template changes only when supported by official release
information or a reproducible non-device test. Do not invent protocol changes
or weaken the credential boundary to make a bump pass.

## 3. Resolve the dependency alerts

Read every open alert of the repository, following `continuation` with the
same parameters until it is `null`:

```bash
guildbotics member repo-info read --person <person> --resource dependency_alerts \
  --subject GuildBotics/GuildBotics --params '{"state":"open","page_size":100}' \
  [--continuation <value>]
```

A failed read is a failure, never zero alerts: report it, leave the locks
alone, and go on with the tools. Dismissed alerts are a human decision and are
not touched.

Each alert names the lock in `manifest_path`, the `package`, the
`affected_versions` range, and the `patched_version`. A lock can hold several
versions of one package, so work per locked version: match each alert to the
versions of its package the lock holds within its `affected_versions`, and
give each such locked version the highest `patched_version` among the alerts
that match it as its target. A locked version that no alert matches is left
alone.

First look at the remote default branch's lock: an alert none of whose
versions there is within its `affected_versions` is fixed where it ships; the
code host has not re-evaluated it yet. Otherwise look at the update branch:
when it already holds every target (an earlier run), this run's PR carries the
fix, and the report says it awaits that PR's merge (and, for a repository that
follows an upstream host, its publishing there). Otherwise raise each locked
version to exactly its target:

| `manifest_path` | Raise it with |
| --- | --- |
| `uv.lock`, `guildbotics/environment/requirements.txt` | `uv lock --upgrade-package '<package>==<target>'` (without the version, uv may raise it past the fix, across a major). See who requires it with `uv tree --frozen --universal --invert --package <package>` (without `--universal` it shows only this machine's platform, and misses a package such as `brotlicffi` that only another Python implementation installs). For a direct dependency, also raise its lower bound in `pyproject.toml` to the target, so that the manifest states the fix. Then export the snapshot's list again (both manifests are one fix) |
| `desktop/package-lock.json` | In `desktop`: a transitive dependency with `npm update <package>` (`npm install <package>` would add it to `package.json` as a direct dependency); a direct one with `npm install <package>@^<target>`, which also raises its range in `package.json`. npm cannot pick the target of a transitive dependency: `npm update` takes the newest version the requirers' ranges admit, which can be a later major (a `*` or `>=1 <3` range), so the check below decides |
| `desktop/src-tauri/Cargo.lock` | In `desktop/src-tauri`: `cargo update -p <package>@<locked_version> --precise <target>` (the locked version tells two versions of the package apart). When cargo cannot write its registry, point `CARGO_HOME` at a writable directory |

```bash
uv export --frozen --no-dev --no-emit-project --no-header --no-annotate \
  --no-hashes --prune microsandbox --prune wakepy \
  -o guildbotics/environment/requirements.txt
```

This is the export `tests/guildbotics/environment/test_requirements.py` checks.

Then read every version of the package the lock now holds (`uv tree --frozen
--universal --package <package>`, `npm ls <package> --all`, or the lock
entries) and compare each with the one it replaced. An alert is resolved only
when each locked version it matched is now at or above that version's target
and in the same major as before
(for a `0.x` version, the same minor): moving to another major is a human
decision, whichever version the tool picked. A command that ran is not a fix:
uv and npm succeed without changing anything when what requires the package
does not admit the target, and `npm update` succeeds when it moves
past the major. Decide on the final lock, after every alert of that lock has
been raised.

Leave an alert open and report why when:

- it has no `patched_version`;
- its final locked version is not resolved as above: the target is in
  another major, the packages that require it do not admit it, a direct
  dependency's range in `pyproject.toml` or `package.json` has an upper bound
  that excludes it, or the tool picked a version in another major;
- reaching it needs changes beyond the manifests and locks, or the non-device
  checks fail with it;
- its `manifest_path` is not one of the above.

To leave a raised package open again, restore the lock, its manifest, and for
`uv.lock` the exported `requirements.txt` from the update branch, then raise
the other alerts of that lock again.

After the npm changes, read `npm audit` in `desktop` to confirm that the raised
packages are no longer reported. It exits non-zero while any finding remains,
including the alerts left open; its exit status is not a check result.

## 4. Documentation and non-device quality checks

- `docs/native_agent_runtime.ja.md` and `.en.md` carry most of the hits above;
  keep ja and en in step, then run `lychee` as `AGENTS.md` says.
- Use Python 3.15. After changing `uv.lock`, run
  `uv sync --extra test --extra dev` so that the checks use the new lock;
  otherwise use the repository's existing dependencies. Run these checks:

```bash
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
uv run --no-sync python -m pytest tests/guildbotics/intelligences -m 'not real_device'
```

When the Node.js pin or `uv.lock` changed, also run the snapshot's tests:

```bash
uv run --no-sync python -m pytest tests/guildbotics/environment/test_snapshot.py tests/guildbotics/environment/test_requirements.py -m 'not real_device'
```

When `uv.lock` changed, also run `uv run --no-sync lint-imports` and the whole
suite, `uv run --no-sync python -m pytest tests/ -n auto -m 'not real_device'`:
a library is used throughout the code. When `desktop/package-lock.json`
changed, run `npm ci` and `npm run quality` in `desktop`. When
`desktop/src-tauri/Cargo.lock` changed, run `cargo check` in
`desktop/src-tauri`; no CI job builds the Tauri app, so this is its only
check. `scripts/desktop-test-rust.sh` is optional. A Rust build that cannot
finish in the environment's resources is recorded as an unperformed check
(below), not as a failure of the package.

Do not enable real-device opt-ins. The marker expression excludes every
real-device test. Add non-device regression tests for any adapter behavior
changed in this update.

Fix failures caused by this update, or restore the affected tool's pin or
leave the affected alert open (section 3) with the associated changes, then
rerun the affected checks
on the final patch. Follow
AGENTS.md when diagnosing pre-existing failures: before writing an Issue draft,
search the same tracker using `member ticket read --resource tracker_tickets`
with `state: all`, following every continuation with identical conditions.
A failed or incomplete search is not no matches. If an open ticket covers the
issue, comment there with the new findings and report its URL instead of a
draft. If only a closed ticket matches, do not reopen or comment on it;
reference it in Related in a complete new draft. If none matches, prepare the
draft as before. Put any needed complete draft in the PR or final report,
without publishing an Issue or waiting for a reply.
If checks cannot run in the available environment, preserve the patch and
record the exact unperformed checks and reason in the PR.

## 5. Create the PR and report

Commit and push only this update's changes and create or update its PR.
Do not stop at a local diff or ask permission to push or create the PR. An
update branch that holds pins or fixes the default branch lacks always ends
with an open PR, also when this run committed nothing: `member pr create`
returns the open PR of that head and base, or opens one (after a PR closed
without a merge, say so in the body). If the default branch already has
everything, finish with the reasons and no PR. If the code host is
unavailable, keep the local changes and report the specific blocker.

The body lists each tool's old, latest and adopted version with official
sources; each alert resolved (number, package, old and new version, lock),
awaiting this PR's merge or the code host's re-evaluation, or left open with
its reason; the non-device checks
and their results; held pins and their reasons; historical observations left
unchanged; and release-note changes needing attention. When a tool's pin
changed, explicitly state: **Real-device validation and snapshot build were
not performed, as intended for this unattended workflow.** They are not a
pending task or a condition for creating this PR. Do not call the new pins
"verified" on the strength of unit tests.
Immediately after the unverified statement, add: **For optional real-device
verification during review, request `$verify-ai-cli-tools` on this PR.**

After the merge, each device's snapshot turns `stale` when a pin, Node.js, or
`requirements.txt` changed: a running service rebuilds it once it runs the new
code, and `guildbotics environment build` rebuilds it by hand. The code host
re-evaluates the alerts on the merged locks; the next run reads what is still
open.

Report the version comparison, the alerts resolved, awaiting, and left open, check
results, held tools, and PR link. When nothing needs updating or attention, do
not send a notification if the runner supports silent completion.
