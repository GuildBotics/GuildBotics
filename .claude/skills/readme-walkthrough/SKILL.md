---
name: readme-walkthrough
description: Walk through README.md (or README.ja.md) top to bottom against the real app and CLI, and report discrepancies between the documentation and actual behavior. Use when asked to verify README accuracy, detect README drift, or run the README walkthrough test.
---

# README Walkthrough Test

Read the target README from top to bottom, perform the documented steps as a brand-new user would, and produce a **discrepancy report**. The deliverable is the report, not a pass/fail result and not fixes: do NOT edit the README or the source code during the walkthrough. Fixing findings is a separate, user-approved step.

## Ground Rules

- **The README is the test input.** Re-read it fresh at the start of every run and follow its wording literally. Do not "correct" a step from your own knowledge of the codebase — if the documented step does not work as written, that IS a finding.
- **Never touch the user's real environment.** All runtime state must live in temp directories: run the backend and every `guildbotics` CLI command with a temp workspace, a temp `HOME`, and `GUILDBOTICS_SECRETS_BACKEND=env-file`. Never write to the real `~/.guildbotics`, the OS keychain, or the user's active workspace.
- **Repository is read-only** during the walkthrough. The only file that may appear inside the repo is the report under `tmp/`, which is gitignored and so cannot be committed by accident; the harness writes its stack context file into the OS temp dir, outside the repo. The report lives in `tmp/` because it is a deliverable for the human to open, not an input any agent should later consult as source of truth — AGENTS.md's "do not reference gitignored paths" rule is about what counts as repository truth, and does not forbid writing scratch output there.
- **Do not run repo-wide quality checks** (ruff / mypy / pytest / npm quality). This is a documentation test, not CI.
- **The app picks the target README, not an argument.** Start the GUI stack first (step 2) and read the **Display language** the app boots with — it follows the OS locale. Japanese → `README.ja.md` is the primary target; English → `README.md`. **Do not switch the app's display language to match a README**: a Japanese UI is walked against the Japanese README, so that documented labels and actual labels are comparable. Record the observed language and the resulting target at the top of the report.
- Sections may be scoped: an argument like `quick-start`, `slack`, or a section heading limits the walkthrough to that part. Default is the full README. Scope arguments never change the target language.

## Verification Tiers

Classify every documented step into one of these tiers, and label each finding accordingly.

### Tier 1 — Execute for real

- All `guildbotics` CLI commands that only read state or write inside the temp workspace/HOME (`workspace status/current/use`, `secrets status/export/import`, `run <command>`). The README says `uv tool install guildbotics`; do not install — run `uv run guildbotics ...` from the repo root instead and note the substitution once in the report.
- The GUI flow from initial setup through Quick Start, via the e2e stack (below).
- **LLM API key.** The LLM section of initial setup is **required** — setup cannot be completed without a key, so this decides how far every later step gets. If the shell environment provides a real key (`GOOGLE_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`), use it and execute Quick Start's run button for real; assert only that a translated-looking text appears in the output area, never its exact wording. The key goes only into the local app backed by the temp workspace and is discarded with it. If no key is available, **enter an obvious dummy (`sk-readme-walkthrough-dummy`) to finish setup and keep walking** — the run then fails with a provider auth error, which counts as *plumbing verified* (command resolution, child commands, prompt build, provider call) and *output* `unverified: no LLM API key`. Never pull a key from the user's real workspace, `.env`, or keychain.
- File-layout claims: after each GUI/CLI action, check that the files the README says are written (`.env`, `.guildbotics/config/team/project.yml`, `intelligences/`, sample commands, `person.yml` fields) actually exist with the documented shape.

### Tier 2 — Verify up to the external boundary (GitHub / Slack)

Never contact GitHub or Slack and never open their URLs. Instead:

- **Static cross-check against source.** The README makes concrete claims that the code can confirm without any account: GitHub App permissions (Contents / Issues / Projects / Pull requests Read & Write, Organization Projects), PAT scopes (`repo` + `project`), the Slack App manifest content (scopes, Socket Mode, event subscriptions), and where settings are saved (`person.yml` `message_channels`, env var names like `{PERSON_ID}_SLACK_BOT_TOKEN`). Locate the authoritative source by searching (starting points, may have moved: `guildbotics/integrations/slack/app_manifest.py`, `guildbotics/editions/simple/setup_service.py`, `desktop/src/setup/GitHubAppRegistration.tsx`, `desktop/src/setup/SlackAppRegistration.tsx`) and compare with the README text.
- **GUI transitions in the e2e stack.** Confirm the screens, modes, and wording the README describes exist (the new-app / existing-app registration modes, the token fields, the Slack token verification button, lane mapping, Patrol settings) and follow the flow up to — but not including — the point where a real external URL would open or a real credential would be needed. **Name controls by function here, never by literal label**: the labels are exactly what is under test, so a label quoted in this skill goes stale and produces a phantom finding. For the Slack token verification button, only the invalid-token path may be exercised — paste obviously fake `xoxb-` / `xapp-` values and assert that the rejection is surfaced.
- **Entering a GitHub Project URL calls GitHub.** The lane-mapping and `Agent`-field controls fetch project status options as soon as a valid URL is present, so leave that field empty and verify those two claims against the source instead.

### Tier 3 — Out of scope (declare, don't attempt)

Real GitHub/Slack accounts and workspaces, OS keychain writes, AI CLI tool authentication, building the Tauri desktop app, and the first-launch installs it performs (`~/.guildbotics/bin/guildbotics`, `~/.local/bin` shim, skill installation). Where the README makes checkable claims about these (paths, behaviors), do a static source cross-check; otherwise list the step under "unverified (out of scope)" in the report so the manual release checklist can cover it.

## Procedure

### 1. Prepare

Confirm prerequisites once: `uv sync --extra test --extra dev` has been run at the repo root and `npm ci` in `desktop/` (run them if missing). Do not read the README yet — which one is the target is not known until the app is running.

### 2. Start the GUI stack (unseeded)

Do NOT seed: the README's Initial Setup section is itself under test, so the app must land on first-setup. From `desktop/`, run in the background:

```bash
GUILDBOTICS_E2E_STACK=readme \
GUILDBOTICS_E2E_BACKEND_PORT=8768 \
GUILDBOTICS_E2E_FRONTEND_PORT=1423 \
GUILDBOTICS_E2E_TOKEN=readme-walkthrough \
node e2e/start-stack.mjs
```

The harness creates a fresh temp workspace and temp HOME on its own and prints both the workspace path and the path of its stack context file (`<OS tmpdir>/guildbotics-e2e/readme.json`, named after the stack); read that file for the exact paths and reuse that workspace for the CLI steps. Browse the app at `http://127.0.0.1:1423`. The distinct stack name, ports, and token keep it from colliding with a real `npm run e2e` or the user's own desktop backend.

**Driving the app** (each of these costs a wasted turn to rediscover):

- Click by the `ref_N` handles from `read_page`, never by screenshot coordinates — the screenshot is reported at a smaller size than the real viewport, so coordinate clicks silently miss.
- Mantine selects and comboboxes ignore `form_input` (React state never updates): open the control, then click the matching `[role=option]`.
- Read wording with `get_page_text` / `read_page`. Popovers and tooltips render in a portal outside `<main>`, so they may need a DOM query to read.
- The page scrolls inside `section.workspace`, not the window; scroll that element to reach fields below the fold.

### 3. Pick the target README and build the checklist

Read the **Display language** the app booted with and select the README it names (see Ground Rules) — do not change the selector. Then read that README end to end and turn it into an ordered checklist of verifiable claims/steps, each tagged with its tier. **The number of checklist items is the step count the report asks for**, so the figure stays comparable between runs.

### 4. Walk the README in order

For every step, record: README section (+ line number), what the README says, what you did, what actually happened, and a verdict — `match` / `mismatch` / `unverified: <reason>` / `out-of-scope`.

Section-specific notes:

- **Getting Started → Initial Setup**: follow the GUI literally, answering that GitHub will not be used, as the README suggests for a first run. Use the harness temp workspace as the workspace folder. After setup, diff the actual files on disk against the README's list.
- **Quick Start**: per Tier 1 above. Also check the shortcut-related claims as far as the GUI shows them (Setup → Shortcuts existence; the hotkey chip in the command bar).
- **Work Together with a Member**: mostly Tier 3 (skill installation); statically confirm the claims about skill install locations against the desktop/Tauri source, and confirm the skill status display under **Setup → LLM / AI CLI tools** exists.
- **Delegate GitHub Tickets / Ask for Work in Slack**: Tier 2. Include the lane-mapping description, the `Agent` field claim, and the `codex doctor` snippet (existence of the command is out of scope; just note it).
- **Service screen**: confirm the three execution sources and their per-source toggles, the patrol interval and consecutive-failure inputs, the run/stop controls, and any streaming or activity view the README claims, all as described. Beware the trap here: first-setup **forces one active member**, and that member inherits whichever real AI CLI tool was detected on PATH — so pressing Run as-is would hand real work to a real agent. Stubbing the tool is no longer an option — the one-shot `script:` path was removed, and `intelligences/cli_agent_mapping.yml` now only points at catalog tools (`cli_agents/<tool>/default.yml`), so a fake entry cannot stand in for a real CLI. Keep the run OFF the agent path instead, by giving the member a shell routine command, which the scheduler runs with no AI CLI tool involved:

  ```bash
  # $WS = workspaceDir from the readme stack context file
  mkdir -p "$WS/.guildbotics/config/commands"
  printf '#!/bin/sh\nsleep 60\n' > "$WS/.guildbotics/config/commands/sleepy.sh"
  ```

  Then add `routine_commands: [sleepy]` to the member's `$WS/.guildbotics/config/team/members/<person_id>/person.yml`, reload the app, and press Run: a routine fires once right away, whatever the patrol interval says. The 60-second sleep is what makes the stop path observable — a force-stop control renders only while a stop is in progress, so press stop while the routine is still running. Confirm the cancellation in `$WS/.guildbotics/data/run/diagnostics.jsonl` (a `command.started` record with no matching `command.finished` / `command.failed`) rather than by looking for the `sleep` process, which is easy to misattribute to an unrelated one. If you skip this setup, mark the start/stop claims `unverified` **and say so explicitly in the report** — this is the single claim most likely to go untested, so never leave the reader guessing.
- **Create Your Own Commands**: compare the embedded `translate` source in the README with the actual generated `.guildbotics/config/commands/translate.md` in the temp workspace; verify the command-location priority claims against `guildbotics/utils/fileio.py`.
- **Operations Reference / Troubleshooting**: execute the Tier 1 CLI snippets against the temp workspace with `HOME=<temp home>`; statically verify paths, env var names, and defaults (e.g. transcript retention, diagnostics locations) against the source.

### 5. Clean up

Killing the wrapper is not enough — both children survive it, and each needs its own kill matched on **this** run's port so the user's own desktop backend (random port) and any real `npm run e2e` are never touched:

```bash
pkill -f "guildbotics.app_api --host 127.0.0.1 --port 8768"
```

```bash
pkill -f "vite --host 127.0.0.1 --host 127.0.0.1 --port 1423"
```

(The duplicated `--host` in the vite command line is what `start-stack.mjs` actually passes; match it or the pattern misses.) Then confirm both ports are free with `lsof -nP -iTCP:8768 -iTCP:1423 -sTCP:LISTEN` (the harness removes its own stack context file on shutdown), and delete any scratch files that captured secrets (for example an exported `secrets.env`). Temp dirs under the OS tmpdir may be left for the OS to reclaim. Finish with `git status --short` to prove the repo is untouched apart from the report.

### 6. Report

Write the report in Japanese to `tmp/readme-walkthrough-report-<YYYYMMDD>.md` and summarize the highlights in chat. Structure:

1. **サマリ**: 対象 README とそれを選んだ根拠（アプリの表示言語）、実行日、検証ステップ数（手順 3 のチェックリスト項目数）と verdict 別の内訳、環境上の注記（`uv run` 置き換え、LLM キーの有無とダミー使用、secrets backend の強制など)
2. **齟齬 (mismatch)**: 各項目に README の該当行、README の記述、実際の挙動、修正すべき側の推定(ドキュメント側か実装側か)を書く
3. **未検証 (unverified / out-of-scope)**: 理由つき一覧。手動リリースチェックリストの入力になる
4. **補足**: 齟齬を見つけたら、もう一方の言語の README（`README.md` ⇔ `README.ja.md`)の対応箇所も確認し、同じ齟齬があるかを併記する

Do not fix anything. Propose fixes only after the user has read the report and asked for them.
