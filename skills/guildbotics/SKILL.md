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

Use the returned non-secret context for role, profile, GitHub username, proxy signature, and `communication_style`.
Treat the returned member context as the source of truth for the member's persona, role, profile, judgment criteria, and communication style.
The context output also includes a `capabilities` section: this is the authoritative list of every `guildbotics member ...` command (GitHub, git, and Slack) and the cross-cutting rules. Treat it as the source of truth for what you can run, regardless of which task you are doing. The same reference can be reprinted any time with `"$HOME/.guildbotics/bin/guildbotics" member help`.
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

## Memory Flow

The `member context` output includes a `memory` block: `pinned` contains always-on notes, including team policy, and must be treated as standing rules for this session. `digest` contains recently used or updated notes as compact titles and summaries; use it as a hint that a relevant note may exist.

Before working a task, find prior notes. Recall (search) and get (read) are separate:

- When the user asks what the member remembers, recorded, learned, or previously discussed, memory is the primary source. Recall/get memory before inspecting external sources such as GitHub or Slack. Use external sources to verify freshness, when the user asks for the current external state, or when the task requires current state.
- When multiple sources are relevant, synthesize them instead of treating the first source as final. Memory may contain prior context, rationale, and progress not visible in GitHub/Slack; GitHub, Slack, and code may contain the current external state. If sources differ, prefer the newest timestamped information for narrative progress, but prefer the current owning system for canonical state fields such as GitHub issue state, assignees, labels, PR links, Slack thread contents, and code behavior. Clearly separate memory-derived context from current external state when both matter.
- Recall by source, always when a ticket or thread URL is known: `guildbotics member memory recall --person <person_id> --query <ticket_or_thread_url> --meta-only`.
- Recall by topic when prior notes seem likely: take key terms from the ticket/thread, such as feature names, error codes, and identifiers, then add synonyms and EN-JA variants in one OR call, for example `--query リトライ --query 再試行 --query retry`.
- Get only promising hits: from `digest` and recall results, read the few useful notes in full with `guildbotics member memory get`. If nothing looks relevant, do not get.

While working, reality-check each note you read against the current code, ticket, or thread. Do not trust a note blindly.

Before finishing, maintain memory:

- A note was wrong: `guildbotics member memory update --person <person_id> --id <doc-id> ...`
- A note was correct and actually helped: `guildbotics member memory touch --person <person_id> --id <doc-id>`
- You learned something durable: `guildbotics member memory record --person <person_id> --scope personal --title ... --ticket <url> ...`
- The user says "remember this", "this is wrong, fix it", or "raise this to the team": record, update, or promote on their behalf. Never ask the user to edit files.

Policy is pinned team memory (`kind: policy`) and is human-gated. Change it only on the user's instruction or approval, using `member memory update ... --policy-approved` and `--set <key>=<value>` for parameters such as `digest_n=30`. When you think policy should change, propose the change in your reply and apply it only after the user approves.

## Workspace Rules

Treat the user's currently open repository as the shared pair-programming workspace.
Do not run `member git prepare` or clone into the member workspace unless the user explicitly asks for an isolated workspace.
Do not switch branches, reset, clean, or pull automatically.
Inspect the current repository, branch, remote, and working tree state. If the current branch or repository does not match the issue/PR work, stop and ask the user before making git workspace changes.

The member git commands only add the member identity and credential. Everything else is plain git that you run yourself:

- Staging: run plain git (`git add`) to choose what goes into a commit. `member git commit` commits only what is already staged.
- Branching: run plain git (`git switch -c <branch_name>`) when the user explicitly asks for a new branch. There is no member command for branches.
- `member git commit` applies the member name/email to that one commit without changing the repository's git config, so the user's own identity is unaffected afterward.
- `member git push` pushes with the member credential.

Edit and test in the current repository. When publishing is appropriate, stage with plain git first, then use the member commands:

```bash
git add -A   # or: git add <paths> to stage only part of your changes
"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current <<'EOF'
<commit message in the GuildBotics project language>
EOF
"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current
```

Run `member git commit` without `member git push` when the user asks for a local commit only.

## GitHub Issue Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. Follow Memory Flow: source recall by issue URL, topic recall when useful, then get only promising hits.
3. `"$HOME/.guildbotics/bin/guildbotics" member github issue inspect --person <person_id> --url <issue_url>` when the task needs current GitHub state or implementation work.
4. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
5. Edit files in the user's current repository.
6. Run relevant tests or checks.
7. Maintain memory for notes you used, corrected, or newly learned.
8. If code changed, stage it with plain git (`git add -A`, or `git add <paths>` for a partial commit), write a commit message in the GuildBotics project language, and run `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current` with the commit message supplied on stdin.
9. If code changed for an issue and a PR is needed, run `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`, then create the PR with `--content-stdin` so the user can review the PR title/body in the approval prompt:

```bash
"$HOME/.guildbotics/bin/guildbotics" member github pr create --person <person_id> --repo <owner/repo> --head <current_branch> --base <target_branch> --content-stdin --issue-url <issue_url> <<'EOF'
<neutral PR title in the GuildBotics project language>

<neutral PR body in the GuildBotics project language>
EOF
```

Omit `--base` only when the repository default branch is the intended PR target.
10. Post the final issue comment in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github issue comment`, or leave a reaction if no action is needed.

## PR Review Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github pr inspect --person <person_id> --url <pr_url> --include-comments`
3. Follow Memory Flow: source recall, topic recall when useful, then get only promising hits.
4. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
5. If the current branch/repository is not the PR head branch/repository, ask the user before making git workspace changes.
6. Address valid review comments and run relevant checks in the current repository.
7. Maintain memory for notes you used, corrected, or newly learned.
8. Stage changes with plain git (`git add -A`, or `git add <paths>` for a partial commit), then commit with `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current`, supplying the commit message on stdin in the GuildBotics project language.
9. Push updates with `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`.
10. Reply to inline review threads in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github pr reply --reply-target-id <reply_target_id>`.
11. If no change is needed, leave a reply or reaction so the workflow has observable evidence.

## Slack Chat Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. Use the returned `communication_style.interactive_replies` and member profile to draft Slack text in the active member's voice.
3. Prefer human-friendly Slack inputs. Use `--channel-name` for channel names such as `general` or `#general`, and use `--message-url` for Slack message links instead of asking the user for channel IDs, `thread_ts`, or `message_ts`.
4. When the user asks you to inspect or respond to an existing Slack message/thread, read it first:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect thread --person <person_id> --service slack --message-url <slack_message_url>
```

5. Follow Memory Flow after inspecting a thread or channel: source recall when a thread URL is known, topic recall when useful, then get only promising hits.
6. When the user asks about recent channel discussion, read the channel first. Convert natural-language date ranges to Slack timestamps before calling the command:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect channel --person <person_id> --service slack --channel-name <channel_name> --oldest-ts <oldest_ts> --latest-ts <latest_ts> --limit <limit>
```

7. Maintain memory for notes you used, corrected, or newly learned before posting the final response.
8. Post a thread reply with a Slack message URL and a body file or stdin:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat reply --person <person_id> --service slack --message-url <slack_message_url> --body-stdin <<'EOF'
<Slack reply in the member's voice>
EOF
```

9. Post a channel message only when a normal channel post is intended:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat post --person <person_id> --service slack --channel-name <channel_name> --body-stdin <<'EOF'
<Slack message in the member's voice>
EOF
```

10. For reaction-only responses, use semantic reactions only:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat reaction add --person <person_id> --service slack --message-url <slack_message_url> --reaction ack|agree|celebrate|support
```

Do not expose Slack emoji implementation names, Slack tokens, or raw Slack API payloads.

## Workflow Marker Guardrail

If a prompt contains `guildbotics_execution_mode=workflow`, treat that prompt as the primary workflow contract. Do not replace or infer workflow steps from this skill.
Workflow runs are non-interactive and use isolated member workspaces. In that mode, follow the workflow prompt for repository preparation, inspect/read commands, write commands, and the required completion command. Do not use `--workspace-mode current` in workflow runs.
Do not return success from a workflow run until the workflow prompt's required `guildbotics member ... complete` command has succeeded. If completion fails, add the missing evidence required by the workflow prompt or fail the agent run.
