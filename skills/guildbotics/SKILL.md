---
name: guildbotics
description: Work as a configured GuildBotics member through the guildbotics member CLI.
---

# GuildBotics Member Capability

Use this skill when the user asks you to act as a GuildBotics member, handle a GitHub issue or pull request through GuildBotics, publish GitHub/git work as a configured member, or post/reply/react in Slack as a configured member.

## Required First Step

Run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>
```

Use the returned non-secret context for role, profile, GitHub username, proxy signature, available commands, and `communication_style`.
Treat the returned member context as the source of truth for the member's persona, role, profile, judgment criteria, and communication style.
If `communication_style` is present, follow it directly:
- Use `communication_style.interactive_replies` for interactive progress updates and final replies to the user.
- Use `communication_style.github_comments` for GitHub issue comments, PR conversation comments, and PR review thread replies.
- Use `communication_style.neutral_documents` for document-like artifacts and durable records such as issue titles/bodies, PR titles/bodies, commit messages, and task summaries.
- Use `communication_style.machine_outputs` for command output, command arguments, IDs, paths, workflow completion JSON, and workflow `AgentResponse.message`.
Interactive progress updates and final replies are conversational outputs, not neutral task summaries.
Do not flatten the active member voice into the default CLI assistant voice unless `communication_style.machine_outputs` applies.
If the user asks to verify credentials or include a credential check, run:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id> --check-credentials --format json
```

This credential check is allowed: it performs a read-only GuildBotics member credential probe and does not perform GitHub or git writes.

## Active Member Session Rules

After running `guildbotics member context --person <person_id>`, treat that member as the active GuildBotics member for the rest of the conversation/session.
Use the active member for interactive replies to the user, all `guildbotics member ... --person <person_id>` commands, GitHub issue/PR comments, commit, push, and PR creation through member capabilities.
Once an active member is established, write interactive replies in that member's conversation style unless the response is machine-readable control output or a neutral document artifact.
Do not ask the user to repeat the person ID while an active member is established.
Do not switch to another member unless the user explicitly asks to switch members or clear the active member.

## Safety Rules

- Do not use direct GitHub, git, or Slack write commands such as `gh`, raw GitHub/Slack token/API writes, `git commit`, `git push`, or raw Slack HTTP calls.
- All GitHub, git, and Slack writes must go through `guildbotics member ... --person <person_id>`.
- Do not display, infer, store, or copy secrets.
- Use the active GuildBotics workspace configured by the desktop app or `guildbotics workspace use <path>`.
- If `guildbotics member ...` reports that no active workspace is configured, ask the user to select a workspace in GuildBotics desktop or run `guildbotics workspace use <path>`.
- Prefer the desktop-managed CLI at `$HOME/.guildbotics/bin/guildbotics`. Use bare `guildbotics` only if that managed CLI is unavailable.

## Workspace Rules

Treat the user's currently open repository as the shared pair-programming workspace.
Do not run `member git prepare` or clone into the member workspace unless the user explicitly asks for an isolated workspace.
Do not switch branches, create branches, reset, clean, or pull automatically.
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

## GitHub Issue Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github issue inspect --person <person_id> --url <issue_url>`
3. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
4. Edit files in the user's current repository.
5. Run relevant tests or checks.
6. If code changed, write a commit message in the GuildBotics project language and run `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current` with the commit message supplied on stdin.
7. If code changed for an issue and a PR is needed, run `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`, then create the PR with `--content-stdin` so the user can review the PR title/body in the approval prompt:

```bash
"$HOME/.guildbotics/bin/guildbotics" member github pr create --person <person_id> --repo <owner/repo> --head <current_branch> --base <target_branch> --content-stdin --issue-url <issue_url> <<'EOF'
<neutral PR title in the GuildBotics project language>

<neutral PR body in the GuildBotics project language>
EOF
```

Omit `--base` only when the repository default branch is the intended PR target.
8. Post the final issue comment in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github issue comment`, or leave a reaction if no action is needed.

## PR Review Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github pr inspect --person <person_id> --url <pr_url> --include-comments`
3. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
4. If the current branch/repository is not the PR head branch/repository, ask the user before making git workspace changes.
5. Address valid review comments and run relevant checks in the current repository.
6. Commit changes with `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current`, supplying the commit message on stdin in the GuildBotics project language.
7. Push updates with `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`.
8. Reply to inline review threads in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github pr reply --reply-target-id <reply_target_id>`.
9. If no change is needed, leave a reply or reaction so the workflow has observable evidence.

## Slack Chat Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. Use the returned `communication_style.interactive_replies` and member profile to draft Slack text in the active member's voice.
3. Prefer human-friendly Slack inputs. Use `--channel-name` for channel names such as `general` or `#general`, and use `--message-url` for Slack message links instead of asking the user for channel IDs, `thread_ts`, or `message_ts`.
4. When the user asks you to inspect or respond to an existing Slack message/thread, read it first:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect thread --person <person_id> --service slack --message-url <slack_message_url>
```

5. When the user asks about recent channel discussion, read the channel first. Convert natural-language date ranges to Slack timestamps before calling the command:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect channel --person <person_id> --service slack --channel-name <channel_name> --oldest-ts <oldest_ts> --latest-ts <latest_ts> --limit <limit>
```

6. Post a thread reply with a Slack message URL and a body file or stdin:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat reply --person <person_id> --service slack --message-url <slack_message_url> --body-stdin <<'EOF'
<Slack reply in the member's voice>
EOF
```

7. Post a channel message only when a normal channel post is intended:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat post --person <person_id> --service slack --channel-name <channel_name> --body-stdin <<'EOF'
<Slack message in the member's voice>
EOF
```

8. For reaction-only responses, use semantic reactions only:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat reaction add --person <person_id> --service slack --message-url <slack_message_url> --reaction ack|agree|celebrate|support
```

Do not expose Slack emoji implementation names, Slack tokens, or raw Slack API payloads.

## Workflow Marker Guardrail

If a prompt contains `guildbotics_execution_mode=workflow`, treat that prompt as the primary workflow contract. Do not replace or infer workflow steps from this skill.
Workflow runs are non-interactive and use isolated member workspaces. In that mode, follow the workflow prompt for repository preparation, inspect/read commands, write commands, and the required completion command. Do not use `--workspace-mode current` in workflow runs.
Do not return success from a workflow run until the workflow prompt's required `guildbotics member ... complete` command has succeeded. If completion fails, add the missing evidence required by the workflow prompt or fail the agent run.
