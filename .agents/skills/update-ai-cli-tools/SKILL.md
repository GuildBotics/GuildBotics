---
name: update-ai-cli-tools
description: Update GuildBotics' pinned AI CLI tools to the latest stable releases and create a PR unattended, without real-device validation.
---

# Updating the pinned AI CLI tools

Invoke with `$update-ai-cli-tools` or "AI CLIツールを更新して".
Check the official releases, update the pins, run the non-device quality
checks, and create a Draft PR without asking for confirmation. Invoking this
update workflow includes committing and pushing its branch and creating or
updating its PR. Do not merge it.

GuildBotics pins its provisioned tools in `CliAgentProvision` in
`guildbotics/intelligences/cli_agents.py`. This workflow updates those pins;
it does not establish that the new releases work on a real device.

## Scope

- **By default, raise every tool to its latest release.** Raise only some of
  them when the user names the tools. "Latest" is what the tool's own default
  install gives: the npm `latest` dist-tag, Grok Build's `stable` channel, the
  Antigravity manifest. Never a prerelease, alpha, or `next` tag.
- Fetch current versions on every run. If there are no newer releases, finish
  without file changes or a PR.
- One Draft PR for the whole bump, separate from any other change. Check open
  update PRs first: reuse this workflow's existing PR and branch, preserving
  others' changes. If an equivalent update is already proposed, report its
  link instead of creating a duplicate.
- A tool whose release cannot be retrieved, or whose update cannot pass the
  non-device checks, stays on its current pin. Explain the failure and raise
  the others. Unperformed real-device checks do not prevent a bump or PR.
- Do not build snapshots, start microVMs, run provider CLIs or real-device
  contract/smoke tests, or use saved provider logins. Do not request approval
  or wait for someone to perform these checks.
- Not touched: `uv.lock`, `desktop/package-lock.json`, `desktop/src`. The tools
  are installed inside the snapshot by npm or an install script, never through
  the repository's dependency management.

## 1. Isolate the update and find the latest versions

Work from the repository's current remote default branch in a dedicated
worktree or checkout, on a `codex/` branch. When continuing this workflow's PR,
use its branch. Keep a checkout served by Desktop or `guildbotics start`
unchanged: those processes can rebuild snapshots from its pins after restart.

| Tool | Latest | Pin in `cli_agents.py` |
| --- | --- | --- |
| Codex | `npm view @openai/codex dist-tags.latest` | `package="@openai/codex@<v>"` |
| Claude Code | `npm view @anthropic-ai/claude-code dist-tags.latest` | `package="@anthropic-ai/claude-code@<v>"` |
| GitHub Copilot | `npm view @github/copilot dist-tags.latest` | `package="@github/copilot@<v>"` |
| Grok Build | `curl -fsSL https://x.ai/cli/stable` | `bash -s <v>` in `install` |
| Antigravity | the two manifests below | versioned URL and both `sha512` in `install` |

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
  -- . ':!tests/guildbotics/intelligences/agent_environment/test_snapshot.py' ':!.agents/skills/update-ai-cli-tools'
# Every full version, and every version-named fixture, in the files that describe the tools
git grep -nIE '(^|[^0-9.])[0-9]+\.[0-9]+\.[0-9]+([^0-9.]|$)|_[0-9]+_[0-9]+_[0-9]+\.json' \
  -- guildbotics/intelligences/cli_agents.py guildbotics/intelligences/agent_runtime \
     guildbotics/templates/intelligences/cli_agents tests/guildbotics/intelligences/agent_runtime \
     'docs/native_agent_runtime.*.md' | grep -v 'node:'
```

`test_snapshot.py` uses `9.9.9` as a made-up pin, the `node:` lines name the
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
  `agent_runtime/codex.py` says so. Preserve this configuration; this workflow
  does not test the new Codex release's bundled helper.

Make adapter or template changes only when supported by official release
information or a reproducible non-device test. Do not invent protocol changes
or weaken the credential boundary to make a bump pass.

## 3. Documentation and non-device quality checks

- `docs/native_agent_runtime.ja.md` and `.en.md` carry most of the hits above;
  keep ja and en in step, then run `lychee` as `AGENTS.md` says.
- Use Python 3.12 and the repository's existing dependencies. Run these checks:

```bash
uv run --no-sync ruff format --check guildbotics tests
uv run --no-sync ruff check guildbotics
uv run --no-sync mypy guildbotics
uv run --no-sync pylint guildbotics
env -u GUILDBOTICS_CONTRACT_PROBE -u GUILDBOTICS_GROK_SMOKE \
  -u GUILDBOTICS_COPILOT_SMOKE -u GUILDBOTICS_ANTIGRAVITY_SMOKE \
  uv run --no-sync python -m pytest tests/guildbotics/intelligences -m 'not real_device'
```

The removed opt-in variables also prevent an inherited setting from enabling
device checks during pytest configuration. Add non-device regression tests
for any adapter behavior changed in this bump.

Fix failures caused by this update or restore the affected tool's pin and
associated changes, then rerun the affected checks on the final patch. Follow
AGENTS.md when diagnosing pre-existing failures: put its complete Issue draft
in the PR or final report, without publishing an Issue or waiting for a reply.
If checks cannot run in the available environment, preserve the patch and
record the exact unperformed checks and reason in the Draft PR.

## 4. Create the PR and report

Commit and push only this update's changes and create or update its Draft PR.
Do not stop at a local diff or ask permission to push or create the PR. If no
tool can be raised, finish with the reasons and no empty PR. If GitHub access
is unavailable, keep the local changes and report the specific blocker.

The body lists each tool's old, latest and adopted version with official
sources, the non-device checks and their results, held pins and their reasons,
historical observations left unchanged, and release-note changes needing
attention. Explicitly state: **Real-device validation and snapshot build were
not performed, as intended for this unattended workflow.** They are not a
pending task or a condition for creating this PR. Do not call the new pins
"verified" on the strength of unit tests.

After the merge, each device's snapshot turns `stale`: a running service
rebuilds it once it runs the new code, and `guildbotics environment build`
rebuilds it by hand.

Report the version comparison, check results, held tools, and PR link. When
nothing needs updating or attention, do not send a notification if the runner
supports silent completion.
