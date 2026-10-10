---
name: verify-ai-cli-tools
description: Verify AI CLI releases on an existing GuildBotics update PR when a human requests real-device checks during review, with approval and observation for each execution.
---

# Verifying the AI CLI tools on an update PR

Invoke with `$verify-ai-cli-tools` and an existing update PR, or use the PR
already identified in the conversation. If the target is unknown, ask which
PR to verify. This is optional, human-attended verification; the unattended
[update skill](../update-ai-cli-tools/SKILL.md) does not call it.

Inspect the PR's current head, changed pins, and existing verification results.
Use that PR's branch and checkout, normally `codex/update-ai-cli-tools`, in a
dedicated worktree. Do not verify the remote default branch or a different
revision, switch the user's shared checkout, create a new update PR, or merge.
Choose the affected tools and checks from the human's request and PR diff.

## 1. Obtain approval and build in a separate workspace

Before each build or real-device test command, show its exact target and
command, what leaves the device, how it affects saved authentication, and
what provider quota it spends. Obtain explicit approval for that execution
and run only while the human can watch. A request to use this skill does not
replace execution-specific approval. Run checks one at a time without
pytest-xdist (`-p no:xdist`), as AGENTS.md requires.

| Execution | External traffic and authentication | Quota |
| --- | --- | --- |
| Snapshot build | Downloads packages from npm, `x.ai`, `storage.googleapis.com`, and the image's package sources; no provider login is used | No inference quota |
| Contract suite | Synthetic credentials and local recorders; no real provider login and nothing sent off the device | None |
| Grok Build, Copilot, Antigravity smoke | Saved device login, provider requests, a turn and resume; Copilot quota and Antigravity usage probes | Spends provider quota |
| Codex or Claude Code one turn | Saved device login and provider requests through the product command path | Spends provider quota |

Snapshot names hash `build_steps()`, so changed pins change the snapshot.
Build in a new verification workspace, never one served by Desktop or
`guildbotics start`: a build removes that workspace's other snapshots, and
a service running old code can rebuild its own snapshot and remove yours.
Saved logins are device-wide, so a separate workspace still uses the device's
logins. Do not log in, reset, or replace them as part of verification.

After build approval, run from the target PR checkout with Python 3.12 and
its dependencies. This creates a disposable workspace outside the repository:

```bash
VERIFICATION_WS="$(mktemp -d "${TMPDIR:-/tmp}/guildbotics-verify.XXXXXX")"
mkdir -p "$VERIFICATION_WS/.guildbotics/config"
export GUILDBOTICS_WORKSPACE_ROOT="$VERIFICATION_WS"
export GUILDBOTICS_CONFIG_DIR="$VERIFICATION_WS/.guildbotics/config"
C="$GUILDBOTICS_CONFIG_DIR"
mkdir -p "$C/team/members/tester" "$C/commands" "$C/intelligences" "$VERIFICATION_WS/work"
printf 'language: en\ndefault_person_id: tester\n' > "$C/team/project.yml"
printf 'person_id: tester\nname: Tester\nis_active: false\nperson_type: agent\n' \
  > "$C/team/members/tester/person.yml"
uv run --no-sync guildbotics environment build
```

Keep both workspace variables set for every command below. The workspace
root takes precedence over the config directory; an inherited root must not
redirect the build or checks to a live workspace. The `tester` member is
needed by the contract product-path tests and every tool's turn checks.
This workspace has no matching Desktop, so its `guildbotics run` commands
execute locally.
The build log shows `grok --version` and `agy --version`. A failed build is
remembered as `<snapshot>.failed` until the pins change or a build runs again;
the service does not retry it.

## 2. Run the approved checks and diagnose failures

The contract suite checks the gateway routes, stand-in formats, refresh,
credential protection, GuildBotics, and member git inside the microVM:

```bash
D=tests/guildbotics/environment
GUILDBOTICS_CONTRACT_PROBE=1 uv run --no-sync python -m pytest -p no:xdist -rs \
  $D/test_provider_contracts.py $D/test_credential_boundary.py \
  $D/test_guildbotics_in_environment.py $D/test_member_git_in_environment.py
```

The smoke commands check a turn and resume for each selected tool. Copilot's
checks also observe `account.getQuota`; Antigravity's observe `/usage`:

```bash
S=tests/guildbotics/guest/smoke
GUILDBOTICS_GROK_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs $S/test_grok_smoke.py
GUILDBOTICS_COPILOT_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs \
  $S/test_copilot_smoke.py $S/test_copilot_usage_smoke.py
GUILDBOTICS_ANTIGRAVITY_SMOKE=1 uv run --no-sync python -m pytest -p no:xdist -rs $S/test_antigravity_smoke.py
```

Execute only the command approved for the selected tool, then report its
result before obtaining approval for the next one.

Codex and Claude Code have no smoke test. For either selected tool, add this
minimal product-path command to the shared verification setup above:

```bash
printf -- '---\nbrain: agent\n---\n\nReply with exactly the word pong. Do not run any tools.\n' \
  > "$C/commands/ping.md"
```

Select `codex` or `claude`, then obtain approval for that tool's single turn:

```bash
CLI_TOOL=codex
printf 'default: cli_agents/%s/default.yml\n' "$CLI_TOOL" > "$C/intelligences/cli_agent_mapping.yml"
uv run --no-sync guildbotics run ping --person tester --cwd "$VERIFICATION_WS/work"
```

A successful turn prints `pong`. For Codex, the default image has no
bubblewrap, so this turn tests its bundled helper; the log says "Codex could
not find bubblewrap on PATH". Keep the image's bubblewrap exclusion.

Record passed, failed, and skipped tests with their reasons. Exit code zero
does not prove a requested check ran: missing snapshots or unmet prerequisites
can skip real-device tests. Mark skipped or unexecuted checks as unverified.

A contract failure can mean a provider contract changed. With separate
approval, rerun the failed test with `-l -vv` and temporarily inspect
`recorder.seen` in the synthetic-credential contract test. Remove temporary
instrumentation before publishing. When the observed change fits the gateway,
fix `credential_broker` (`routes`, `base_url_env`, `refresh`, `stand_in_*`) and
its regression tests. Never weaken the authentication boundary to pass.
To locate the release that introduced a change, inspect downloaded binaries
with `npm pack @openai/codex@<version>-linux-arm64` and `strings` in a temporary
directory; do not run those binaries or store credentials in the evidence.

For a failed tool, restore its pin to the PR base version and its associated
changes, or ask the human to decide when the remedy is unclear. Diagnose
pre-existing failures and prepare an Issue draft under AGENTS.md; do not
publish an Issue without human authorization. After changing a pin, adapter,
or build recipe, rebuild and rerun affected checks with fresh approval on
the final configuration. A prior passing result is not evidence for that revision.

## 3. Record only observed behavior on the same PR

Use the version searches and distinctions in the update skill's
[version-dependent documentation section](../update-ai-cli-tools/SKILL.md#2-update-what-depends-on-the-version).
Advance measured descriptions, verified baselines, and fixtures only when a
check observed that exact behavior on the final pin. Rename a newly recorded
fixture and its references together; retain earlier evidence that remains useful.
One `pong` response does not verify refresh, quota, usage payloads, or resume.
Keep Japanese and English documentation aligned and check the documented
limitations against the observations. Do not advance unobserved claims.

Run the update skill's non-device quality checks for changes made here and
AGENTS.md's Markdown link checks. Commit and push the verified changes to
the same PR through the project's publishing procedure.

Replace the PR's unattended "not performed" statement with the actual
results: tested revision and final pins, exact commands, passed/failed/skipped
counts and reasons, observations recorded, held pins, and checks still
unverified. Keep untested tools and behaviors explicitly unverified. If the
PR head changed during verification, establish which results still apply;
never attribute results from an earlier pin or adapter to the new one.
Inspect CI and current-head/base readiness after the final push, and leave
the PR for human review and merge.

## 4. Remove the verification workspace

After recording results and diagnostic evidence, wait for every build and
check to finish, including failed or cancelled runs. From the PR checkout,
remove the disposable workspace's snapshots through the product command,
then remove the directory created by `mktemp` above. Device-wide logins are kept.

```bash
GUILDBOTICS_WORKSPACE_ROOT="${VERIFICATION_WS:?}" \
GUILDBOTICS_CONFIG_DIR="$VERIFICATION_WS/.guildbotics/config" \
  uv run --no-sync guildbotics environment remove &&
  rm -rf -- "$VERIFICATION_WS" &&
  unset GUILDBOTICS_WORKSPACE_ROOT GUILDBOTICS_CONFIG_DIR VERIFICATION_WS C
```

If snapshot removal fails or is interrupted, keep the workspace and its
variables for diagnosis; do not delete its directory directly.
