---
name: guildbotics
description: Work as a configured GuildBotics member through the guildbotics member CLI.
---

# GuildBotics Member Capability

Use this skill when the user asks you to act as a GuildBotics member, handle a GitHub issue or pull request through GuildBotics, or publish GitHub/git work as a configured member.

## Required First Step

Run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>
```

Use the returned non-secret context for role, profile, GitHub username, proxy signature, and available commands.
If the user asks to verify credentials or include a credential check, run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id> --check-credentials --format json
```

This credential check is allowed: it performs a read-only GuildBotics member credential probe and does not perform GitHub or git writes.

## Safety Rules

- Do not use direct GitHub or git write commands such as `gh`, raw GitHub token/API writes, `git commit`, or `git push`.
- All GitHub and git writes must go through `guildbotics member ... --person <person_id>`.
- Do not display, infer, store, or copy secrets.
- Use the active GuildBotics workspace configured by the desktop app or `guildbotics workspace use <path>`.
- If `guildbotics member ...` reports that no active workspace is configured, ask the user to select a workspace in GuildBotics desktop or run `guildbotics workspace use <path>`.
- Prefer the desktop-managed CLI at `$HOME/.guildbotics/bin/guildbotics`. Use bare `guildbotics` only if that managed CLI is unavailable.

## Standard GitHub Issue Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github issue inspect --person <person_id> --url <issue_url>`
3. `"$HOME/.guildbotics/bin/guildbotics" member git prepare --person <person_id> --issue-url <issue_url>`
4. Inspect and edit files under the prepared repository in `~/.guildbotics/data/workspaces/<person_id>`.
5. Run relevant tests or checks.
6. If code changed, write a commit message file and run `"$HOME/.guildbotics/bin/guildbotics" member git publish --person <person_id> --repo-path <repo_path> --message-file <file>`.
7. If code changed for an issue, write PR title/body files and run `"$HOME/.guildbotics/bin/guildbotics" member github pr create --person <person_id> --repo <owner/repo> --head <branch> --title-file <file> --body-file <file> --issue-url <issue_url>`.
8. Post the final issue comment with `"$HOME/.guildbotics/bin/guildbotics" member github issue comment`, or leave a reaction if no action is needed.

## Standard PR Review Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github pr inspect --person <person_id> --url <pr_url> --include-comments`
3. `"$HOME/.guildbotics/bin/guildbotics" member git prepare --person <person_id> --issue-url <issue_url> --pr-url <pr_url>`
4. Address valid review comments and run relevant checks.
5. Publish changes with `"$HOME/.guildbotics/bin/guildbotics" member git publish`.
6. Reply to inline review threads with `"$HOME/.guildbotics/bin/guildbotics" member github pr reply --reply-target-id <reply_target_id>`.
7. If no change is needed, leave a reply or reaction so the workflow has observable evidence.

## Workflow Runs

When invoked by `ticket_driven_workflow`, the prompt includes `workflow_run_id`.
Before returning success, run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member task complete --person <person_id> --run-id <workflow_run_id> --ticket-url <issue_url> --status done|asking|blocked --summary-file <file>
```

If this command fails, do not return a successful response. Add the required evidence with a member write command or fail the agent run.
