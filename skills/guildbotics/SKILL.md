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
Treat the returned member context as the source of truth for the member's persona, role, profile, judgment criteria, and communication style.
Use the member's voice for conversational outputs: interactive replies to the user, GitHub issue comments, PR conversation comments, and PR review thread replies.
Use the GuildBotics project language and the project's neutral document style for document-like artifacts and durable records such as issue titles/bodies, PR titles/bodies, commit messages, and task summaries.
Keep machine-readable command output, command arguments, IDs, paths, and workflow completion JSON factual and valid; do not decorate control data or workflow `AgentResponse.message` with persona prose.
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

## Interactive Workspace Rules

Execution mode is determined by explicit GuildBotics markers:

- If the prompt contains `guildbotics_execution_mode=workflow`, this is a non-interactive workflow run.
- Otherwise, when the user invokes this skill directly from a CLI/Desktop coding session, this is an interactive run.

Do not infer execution mode from the client name alone.
In interactive runs, treat the user's currently open repository as the shared pair-programming workspace.
Do not run `member git prepare` or clone into the member workspace unless the user explicitly asks for an isolated workspace.
Do not switch branches, create branches, reset, clean, or pull automatically in interactive mode.
Inspect the current repository, branch, remote, and working tree state. If the current branch or repository does not match the issue/PR work, stop and ask the user before making git workspace changes.
Edit and test in the current repository. When publishing is appropriate, use:

```bash
"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current <<'EOF'
<commit message in the GuildBotics project language>
EOF
"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current
```

This still commits and pushes with the configured GuildBotics member identity and credential. Run `member git commit` without `member git push` when the user asks for a local commit only.
If the user explicitly asks to create a new branch from the current branch, use:

```bash
"$HOME/.guildbotics/bin/guildbotics" member git branch create --person <person_id> --repo-path <current_repo_path> --branch <branch_name> --workspace-mode current
```

## Interactive GitHub Issue Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github issue inspect --person <person_id> --url <issue_url>`
3. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
4. Edit files in the user's current repository.
5. Run relevant tests or checks.
6. If code changed, write a commit message in the GuildBotics project language and run `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current` with the commit message supplied on stdin.
7. If code changed for an issue and a PR is needed, run `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`, then write neutral PR title/body files and run `"$HOME/.guildbotics/bin/guildbotics" member github pr create --person <person_id> --repo <owner/repo> --head <current_branch> --title-file <file> --body-file <file> --issue-url <issue_url>`.
8. Post the final issue comment in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github issue comment`, or leave a reaction if no action is needed.

## Interactive PR Review Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github pr inspect --person <person_id> --url <pr_url> --include-comments`
3. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
4. If the current branch/repository is not the PR head branch/repository, ask the user before making git workspace changes.
5. Address valid review comments and run relevant checks in the current repository.
6. Commit changes with `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current`, supplying the commit message on stdin in the GuildBotics project language.
7. Push updates with `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`.
8. Reply to inline review threads in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github pr reply --reply-target-id <reply_target_id>`.
9. If no change is needed, leave a reply or reaction so the workflow has observable evidence.

## Workflow Runs

When invoked by `ticket_driven_workflow`, the prompt contains `guildbotics_execution_mode=workflow` and includes `workflow_run_id`.
Workflow runs are non-interactive and use isolated member workspaces. In that mode, run `member git prepare`, edit under `~/.guildbotics/data/workspaces/<person_id>`, and use the default member workspace publish mode. Do not use `--workspace-mode current` in workflow runs.
Before returning success, run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member task complete --person <person_id> --run-id <workflow_run_id> --ticket-url <issue_url> --status done|asking|blocked --summary-file <file>
```

If this command fails, do not return a successful response. Add the required evidence with a member write command or fail the agent run.
