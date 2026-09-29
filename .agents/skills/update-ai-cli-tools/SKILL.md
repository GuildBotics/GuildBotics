---
name: update-ai-cli-tools
description: Use when raising the versions of the AI CLI tools (Codex, Claude Code, Grok Build, GitHub Copilot, Antigravity) that GuildBotics pins in the agent environment.
---

# Updating the pinned AI CLI tools

GuildBotics pins each AI CLI tool it provisions in the agent environment
(`CliAgentProvision` in `guildbotics/intelligences/cli_agents.py`; its docstring
says why). This is the procedure for raising those pins. It was last run end
to end in PR #692.

## Scope

- **By default, raise every tool to its latest release.** Raise only some of
  them when the user names the tools. "Latest" is what the tool's own default
  install gives: the npm `latest` dist-tag, Grok Build's `stable` channel, the
  Antigravity manifest. Never a prerelease, alpha, or `next` tag.
- A tool whose latest release fails step 3 stays on its current pin in this
  bump. Open an issue for it with what failed (a human approves the issue), and
  raise the others. The bump does not wait for that tool.
- One PR for the whole bump, separate from any other change.
- Not touched: `uv.lock`, `desktop/package-lock.json`, `desktop/src`. The tools
  are installed inside the snapshot by npm or an install script, never through
  the repository's dependency management.

## 1. Find the latest versions and rewrite the pins

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

## 2. Build the snapshot in a verification workspace

The snapshot name is a hash of `build_steps()`, so new pins make every existing
snapshot `stale`. Build and verify in a workspace of its own, never in one that
a running Desktop or `guildbotics start` serves:

- A build removes the workspace's other snapshots. A service running the old
  pins rebuilds its own snapshot (`SnapshotUpkeep`) and removes yours.
- In a development setup, Desktop and `~/.guildbotics/bin/guildbotics` run from
  this checkout. A running Desktop keeps the old pins in memory; after its next
  restart it rebuilds the snapshot of the user's workspace with whatever pins
  the checkout has. Tell the user before editing the pins.

Logins are per device (`~/.guildbotics/data/agent_environment/<tool>/`), so the
verification workspace uses the device's saved logins without any setup.

```bash
WS=<a new directory outside the repository>
mkdir -p "$WS/.guildbotics/config"
export GUILDBOTICS_CONFIG_DIR="$WS/.guildbotics/config"
uv run --no-sync guildbotics environment build
```

Keep `GUILDBOTICS_CONFIG_DIR` set for every command below: without it, the
smoke tests run against the workspace the device has selected.

The build downloads from npm, `x.ai` and `storage.googleapis.com`; its log shows
`grok --version` and `agy --version`. A failed build is remembered
(`<snapshot>.failed`) until the pins change or `environment build` runs again;
the service does not retry it. When a pin is put back after step 3, build again
and rerun that tool's checks on the final set of pins.

## 3. Real-device checks

Each of these carries the `real_device` marker. Before running any of them, show
the user what runs, what leaves the device and what quota it spends, and get
explicit approval; run them only while the user can watch. Run one at a time,
without `pytest-xdist` (`-p no:xdist`): the credential checks count the microVMs
that appear while they run.

| Check | What it observes | Account and quota |
| --- | --- | --- |
| Contract suite | Each tool's requests with a stand-in login: which reach the gateway, that nothing carrying it goes elsewhere, the stand-in format it accepts, refresh; GuildBotics and member git inside the microVM | None; nothing leaves the device |
| Smoke: Grok Build, GitHub Copilot, Antigravity | A turn and its resume; Copilot's `account.getQuota` and Antigravity's `/usage` payloads | Saved login; spends quota |
| One turn: Codex, Claude Code | The product path of a command (they have no smoke test) | Saved login; spends quota |

**Contract suite — every bump:**

```bash
D=tests/guildbotics/intelligences/agent_environment
GUILDBOTICS_CONTRACT_PROBE=1 uv run --no-sync python -m pytest -p no:xdist -rs \
  $D/test_provider_contracts.py $D/test_credential_boundary.py \
  $D/test_guildbotics_in_environment.py $D/test_member_git_in_environment.py
```

A failure in `test_provider_contracts.py` is a change in the provider's
contract, not a flaky test. Rerun the one test with `-l -vv` to read the CLI's
replies, and print `recorder.seen` in it temporarily to see every request it
made. Fix the tool's `credential_broker` (`routes`, `base_url_env`, `refresh`,
`stand_in_*`) when the change fits the gateway; otherwise the tool stays on its
pin (see Scope). To find the release that introduced a change, look for its
message in each release's binary (`npm pack @openai/codex@<v>-linux-arm64`,
then `strings`).

**Smoke tests — each bumped tool that has one:**

```bash
S=tests/guildbotics/intelligences/agent_runtime/smoke
GUILDBOTICS_GROK_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs $S/test_grok_smoke.py
GUILDBOTICS_COPILOT_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs \
  $S/test_copilot_smoke.py $S/test_copilot_usage_smoke.py
GUILDBOTICS_ANTIGRAVITY_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs $S/test_antigravity_smoke.py
```

**One turn — Codex and Claude Code, whichever was bumped:**

```bash
C="$GUILDBOTICS_CONFIG_DIR"
mkdir -p "$C/team/members/tester" "$C/commands" "$C/intelligences" "$WS/work"
printf 'language: en\ndefault_person_id: tester\n' > "$C/team/project.yml"
printf 'person_id: tester\nname: Tester\nis_active: false\nperson_type: agent\n' \
  > "$C/team/members/tester/person.yml"
printf -- '---\nbrain: agent\n---\n\nReply with exactly the word pong. Do not run any tools.\n' \
  > "$C/commands/ping.md"
for tool in codex claude; do
  echo "default: cli_agents/$tool/default.yml" > "$C/intelligences/cli_agent_mapping.yml"
  uv run --no-sync guildbotics run ping --person tester --cwd "$WS/work"
done
```

The verification workspace is not the one Desktop serves, so `run` executes
locally. Each prints `pong` when the turn works.

## 4. Update what depends on the version

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

- **The pin, or a claim about the pinned version** ("Verified against",
  "verified baselines", "pins", "動作確認済みの基準バージョン", "固定しており"):
  set it to the new pin once step 3 passes for that tool.
- **An observation of one version** ("measured / observed on", "実測", a
  version-named fixture, a comment quoting a tool's output): move it to the new
  pin only when a check in step 3 observed that very thing (the table says what
  each check observes); rename a re-recorded fixture and its references. If the
  behavior changed, fix the code, the fixture and the description. Otherwise
  leave it naming the version it was observed on. A sentence that says an
  observed version is the pinned one ("1.0.86, the version the environment
  pins") is split: the pin moves, the observation stays.
- **A version requirement of the tool** ("before 2.1.246", "1.1.11 以降", "the
  1.0.83 server had no `account.getQuota`"): leave it.
- **Never leave a description measured on a version newer than the pin.** The
  product runs the pin, so such a description says nothing about it. Raising
  every tool to its latest removes the case; re-observe it on the new pin.

Also check by hand:

- `guildbotics/templates/intelligences/cli_agents/*/default.yml`: choices read
  from a tool's own answer (Copilot's effort levels, from `session/new`).
- Codex: `docker/agent-environment/Dockerfile` removes the image's bubblewrap
  because Debian's 0.8.0 could not exec Codex's helper, and
  `agent_runtime/codex.py` says so. The default image has no bubblewrap, so the
  Codex turn in step 3 runs on Codex's bundled one (its log says "Codex could
  not find bubblewrap on PATH"); a passing turn confirms that configuration.

## 5. Documentation and quality checks

- `docs/native_agent_runtime.ja.md` and `.en.md` carry most of the hits above;
  keep ja and en in step, then run `lychee` as `AGENTS.md` says.
- `ruff format --check guildbotics tests`, `ruff check guildbotics`,
  `mypy guildbotics`, and `pytest tests/guildbotics/intelligences`.

## 6. The PR

The body lists each tool's old, latest and new version, the real-device checks
that ran and their result, each tool held on its pin with its issue, and the
observations left on an older version.

After the merge, each device's snapshot turns `stale`: a running service
rebuilds it once it runs the new code, and `guildbotics environment build`
rebuilds it by hand.
