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

## Definition of Done (DOD)

Before sending your final interactive reply to the user, ensure all of the following steps are completed:
1. **Verification**: When code changes are made, run all relevant automated tests, linter, and static analysis checks to ensure correctness.
2. **Staging & Commit**: When code changes are meant to be published, stage your changes (`git add`) and commit/push/publish using the member capabilities (`guildbotics member git commit/push/publish`).
3. **Durable Actions**: Perform requested external writes (create PRs, issue comments, or Slack replies) if applicable.
4. **Memory Preservation**: When a PR or significant changes are published, record or update a memory document with the work details (PR URL, branch name, commit SHA, test results). For other tasks, follow the memory reference guidelines (record durable reusable lessons, touch helped memories, or update incorrect memories).

## Active Member Session Rules

After running `guildbotics member context --person <person_id>`, treat that member as the active GuildBotics member for the rest of the conversation/session.
Use the active member for interactive replies to the user, all `guildbotics member ... --person <person_id>` commands, GitHub issue/PR comments, commit, push, and PR creation through member capabilities.
Once an active member is established, write interactive replies in that member's conversation style unless the response is machine-readable control output or a neutral document artifact.
Do not ask the user to repeat the person ID while an active member is established.
Do not switch to another member unless the user explicitly asks to switch members or clear the active member.

## Safety Rules

- Do not perform GitHub, git, or Slack writes before reading `member context`.
- After reading `member context`, follow its capabilities section for write safety. Publishing writes must go through `guildbotics member ... --person <person_id>`.
- Do not display, infer, store, or copy secrets.
- Use the active GuildBotics workspace configured by the desktop app or `guildbotics workspace use <path>`.
- If `guildbotics member ...` reports that no active workspace is configured, ask the user to select a workspace in GuildBotics desktop or run `guildbotics workspace use <path>`.
- Prefer the desktop-managed CLI at `$HOME/.guildbotics/bin/guildbotics`. Use bare `guildbotics` only if that managed CLI is unavailable.

## Memory Flow

Follow the memory rules in the `capabilities` section returned by `member context`; that section is the source of truth for recall/get/touch/update/record and source-vs-current-state handling.

### Memory Record/Update Guidelines (DOD Requirement)
Maintain memory when appropriate to preserve work context and lessons:
- **PR Creation/Update**: After a PR is created or updated, record a durable work context with `--pr <pr_url>` (and `--ticket <issue_url>` if resolving a ticket, or `--thread <thread_url>` when the work originated from a chat thread) including the branch, commit SHA, verification result, and remaining follow-up. Do this before the interactive final reply.
- **Slack Interaction**: Follow the general memory rules for chat threads: check for existing relevant memory, touch helped memories, and record only when a new durable reusable lesson is learned. Do not create raw duplicates of chat discussions.
- **Technical Lessons**: Record separate technical lessons as reusable memory documents only when they are valuable beyond a single PR or issue.

Interactive sessions have two extra obligations:

- When the user asks what the member remembers, recorded, learned, or previously discussed, memory is the primary source. Recall/get memory before inspecting external sources, then use GitHub, Slack, or code to verify freshness when current state matters.
- The user may directly ask you to remember, correct, promote, or archive memory. Do it through `guildbotics member memory ... --person <person_id>` instead of asking the user to edit files. Policy memory still requires the user's instruction or approval before using `--policy-approved`.

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
If you create or update a PR outside the GitHub Issue Flow or PR Review Flow, maintain memory after the PR exists and before the interactive final reply. Record durable PR work context with `--pr <pr_url>` when known and `--thread <thread_url>` when the work originated from a chat thread, including branch, commit, verification result, and remaining follow-up. Record separate technical lessons as separate memory documents when they are reusable beyond that PR.

## GitHub Issue Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. Follow Memory Flow with the issue URL as the source key.
3. `"$HOME/.guildbotics/bin/guildbotics" member github issue inspect --person <person_id> --url <issue_url>` when the task needs current GitHub state or implementation work.
4. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
5. Edit files in the user's current repository.
6. Run relevant tests or checks.
7. If code changed, stage it with plain git (`git add -A`, or `git add <paths>` for a partial commit), write a commit message in the GuildBotics project language, and run `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current` with the commit message supplied on stdin.
8. If code changed for an issue and a PR is needed, run `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`, then create the PR with `--content-stdin` so the user can review the PR title/body in the approval prompt:

```bash
"$HOME/.guildbotics/bin/guildbotics" member github pr create --person <person_id> --repo <owner/repo> --head <current_branch> --base <target_branch> --content-stdin --issue-url <issue_url> <<'EOF'
<neutral PR title in the GuildBotics project language>

<neutral PR body in the GuildBotics project language>
EOF
```

Omit `--base` only when the repository default branch is the intended PR target.
9. Post the final issue comment in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github issue comment`, or leave a reaction if no action is needed.
10. After commit, push, PR creation, and final GitHub comment/reaction are done, maintain memory according to the member capabilities before the interactive final reply. When a PR was created or updated, record durable context that would help resume the work later with `--ticket <issue_url>` and `--pr <pr_url>` when known, including branch, verification result, and remaining follow-up.

## PR Review Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. `"$HOME/.guildbotics/bin/guildbotics" member github pr inspect --person <person_id> --url <pr_url> --include-comments`
3. Follow Memory Flow with the PR URL as the source key.
4. Inspect the current repository, branch, remote, and working tree state without changing branches automatically.
5. If the current branch/repository is not the PR head branch/repository, ask the user before making git workspace changes.
6. Address valid review comments and run relevant checks in the current repository.
7. Stage changes with plain git (`git add -A`, or `git add <paths>` for a partial commit), then commit with `"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --message-stdin --workspace-mode current`, supplying the commit message on stdin in the GuildBotics project language.
8. Push updates with `"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current`.
9. Reply to inline review threads in the member's voice with `"$HOME/.guildbotics/bin/guildbotics" member github pr reply --reply-target-id <reply_target_id>`.
10. If no change is needed, leave a reply or reaction so the workflow has observable evidence.
11. After commit, push, and PR replies/reactions are done, maintain memory according to the member capabilities before the interactive final reply. When a PR was updated, record durable context that would help resume the review later with `--pr <pr_url>`, including branch, verification result, addressed threads, and remaining follow-up.

## Slack Chat Flow

1. `"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>`
2. Use the returned `communication_style.interactive_replies` and member profile to draft Slack text in the active member's voice.
3. Prefer human-friendly Slack inputs. Use `--channel-name` for channel names such as `general` or `#general`, and use `--message-url` for Slack message links instead of asking the user for channel IDs, `thread_ts`, or `message_ts`.
4. When the user asks you to inspect or respond to an existing Slack message/thread, read it first:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect thread --person <person_id> --service slack --message-url <slack_message_url>
```

5. Follow Memory Flow after inspecting a thread or channel, using the Slack message/thread URL as the source key when available.
6. When the user asks about recent channel discussion, read the channel first. Convert natural-language date ranges to Slack timestamps before calling the command:

```bash
"$HOME/.guildbotics/bin/guildbotics" member chat inspect channel --person <person_id> --service slack --channel-name <channel_name> --oldest-ts <oldest_ts> --latest-ts <latest_ts> --limit <limit>
```

7. Maintain memory according to the member capabilities before posting the final response.
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
