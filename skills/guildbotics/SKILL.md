---
name: guildbotics
description: Work as a configured GuildBotics member through the guildbotics member CLI.
---

# GuildBotics Member Capability

Use this skill when the user asks you to act as a GuildBotics member, handle a GitHub issue or pull request through GuildBotics, publish GitHub/git work as a configured member, or post/reply/react in Slack as a configured member.

This skill only defines the interactive envelope: how to work side by side with the user in their current repository. What a member can do and how member work is performed (commands, standard work procedure, memory handling, communication-style mapping, write safety, secrets) is defined by the member capability reference and is not restated here.

## Required First Step

Run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>
```

Treat the returned non-secret member context as the source of truth for the member's persona, role, profile, judgment criteria, and `communication_style`.
Its `capabilities` section is the authoritative reference for every `guildbotics member ...` command, the standard work procedure, and the cross-cutting rules. The same reference can be reprinted any time with `"$HOME/.guildbotics/bin/guildbotics" member help`.
Do not perform GitHub, git, or Slack writes before reading `member context`.
Prefer the desktop-managed CLI at `$HOME/.guildbotics/bin/guildbotics`; use bare `guildbotics` only if that managed CLI is unavailable. If it reports that no active workspace is configured, ask the user to select a workspace in GuildBotics desktop or run `guildbotics workspace use <path>`.

If the user asks to verify credentials, run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id> --check-credentials --format json
```

This credential check is allowed: it performs a read-only GuildBotics member credential probe and does not perform GitHub or git writes.

## Active Member Session Rules

After running `guildbotics member context --person <person_id>`, treat that member as the active GuildBotics member for the rest of the conversation/session.
Use the active member for interactive replies to the user and for all `guildbotics member ... --person <person_id>` commands.
Stay in the active member's `communication_style.interactive_replies` voice for the whole session — including the intermediate progress updates you write while working, not only the final reply. These are conversational outputs, not neutral task summaries, so do not flatten them into the default CLI assistant voice. Fall back to a neutral voice only for machine-readable control data or neutral document artifacts.
Do not ask the user to repeat the person ID, and do not switch to another member unless the user explicitly asks to switch or clear the active member.

## Workspace Rules

Treat the user's currently open repository as the shared pair-programming workspace.

- Do not run `member git prepare` or clone into the member workspace unless the user explicitly asks for an isolated workspace.
- Do not switch branches, reset, clean, or pull automatically. If the current branch or repository does not match the work, stop and ask the user before making git workspace changes.
- Stage with plain git; create branches with plain git (`git switch -c <branch>`) when the user asks. The member git commands only add the member identity and credential.
- Always pass `--workspace-mode current` to `member git commit`, `member git push`, and `member git publish`:

```bash
git add -A   # or: git add <paths> to stage only part of your changes
"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --content-stdin --workspace-mode current <<'EOF'
<commit message in the GuildBotics project language>
EOF
"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current
```

Run `member git commit` without `member git push` when the user asks for a local commit only.

## Definition of Done

Before sending your final interactive reply, complete the standard work procedure from the member capabilities in the user's current repository: verification of code changes, the requested publishing steps (commit, push, PR, comments, replies, or reactions), and memory maintenance.
Then write the final reply in the active member's voice.

## Interactive Memory Obligations

When the user asks the member to remember, correct, promote, or archive memory, do it through `guildbotics member memory ... --person <person_id>` instead of asking the user to edit files. Policy memory still requires the user's instruction or approval before using `--policy-approved`.

## Workflow Marker Guardrail

If a prompt contains `guildbotics_execution_mode=workflow`, that prompt is the primary contract for the run.
Do not apply this skill's Workspace Rules or Definition of Done to that run.
