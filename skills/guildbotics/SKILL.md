---
name: guildbotics
description: Work as a configured GuildBotics member through the guildbotics member CLI.
---

# GuildBotics Member Capability

Use this skill when the user asks you to act as a GuildBotics member, handle an issue or pull request through GuildBotics, publish repository/git work as a configured member, or post/reply/react in the chat as a configured member.

This skill only defines the interactive envelope: how to work side by side with the user in their current repository. What a member can do and how member work is performed (commands, standard work procedure, memory handling, communication-style mapping, write safety, secrets) is defined by the member capability reference and is not restated here.

## Required First Step

Before any member command, the GuildBotics server must be running on the Hub (`guildbotics server start`, or its OS service). The member CLI delegates to that server, including `--help`; it cannot run alone. Use the managed CLI installed by the same Rust build as the server. On the Hub, the LaunchAgent install/update places it through `guildbotics install`. If you start the server from a terminal, run the built executable's `install` first. On another machine, forward the Hub's port and run `guildbotics connect` first. Member commands use the Hub's selected workspace. On the Hub, `member --workspace <path>` verifies the selected workspace ID on this machine; on another machine, omit `--workspace`, since the CLI reads that ID from the Hub. To change the selection, finish active work and switch it on the Hub's GuildBotics screens, or stop the Hub's server with `guildbotics server stop`, run `guildbotics workspace use <path>` on the Hub, and start it again.

Run the managed CLI by absolute path:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id>
```

Treat the returned non-secret member context as the source of truth for the member's persona, role, profile, judgment criteria, and `communication_style`.
Its `capabilities` section is the authoritative reference for every `guildbotics member ...` command, the standard work procedure, and the cross-cutting rules. The same reference can be reprinted with `"$HOME/.guildbotics/bin/guildbotics" member help`.
Do not perform ticket, repository, git, or chat writes before reading `member context`.
Always use the managed absolute path. If it reports that no active workspace is configured, ask the user to select a workspace on the Hub's GuildBotics screens or run `guildbotics workspace use <path>` on the Hub.

If the user asks to verify credentials, use the same executable:

```bash
"$HOME/.guildbotics/bin/guildbotics" member context --person <person_id> --check-credentials --format json
```

This credential check is allowed: it performs a read-only GuildBotics member credential probe and does not perform repository or git writes.

## Active Member Session Rules

After running `guildbotics member context --person <person_id>`, treat that member as the active GuildBotics member for the rest of the conversation/session.
Use the active member for interactive replies to the user and for all `guildbotics member ... --person <person_id>` commands.
Stay in the active member's `communication_style.interactive_replies` voice for the whole session — including the intermediate progress updates you write while working, not only the final reply. These are conversational outputs, not neutral task summaries, so do not flatten them into the default CLI assistant voice. Fall back to a neutral voice only for machine-readable control data or neutral document artifacts.
Do not ask the user to repeat the person ID, and do not switch to another member unless the user explicitly asks to switch or clear the active member.

## Workspace Rules

Treat the user's currently open repository as the shared pair-programming workspace.

Use the bundled `repository/issue_inspect`, `repository/pr_inspect`, and `repository/pr_checks` commands through the managed CLI's `run` entry. Pass `--person <person_id> url=<ticket_or_pr_url>`; their complete usage and the workflow-internal invocation are in `member context` / `member help`.

- Do not run `member git prepare` or clone into the member workspace. The member's own clones are worked on only inside the isolated environment of a running GuildBotics command, so member-mode git is refused in this session; for isolated work, delegate it as described below.
- Do not switch branches, reset, clean, or pull automatically. If the current branch or repository does not match the work, stop and ask the user before making git workspace changes.
- Stage with plain git; create branches with plain git (`git switch -c <branch>`) when the user asks. The member git commands only add the member identity and credential.
- Always pass `--workspace-mode current` to `member git commit`, `member git push`, and `member git publish`.
- To delegate a one-off task to another member, run `guildbotics run ask --person <person_id> --cwd <current_repo_path>` using the managed CLI above; write the request to a temporary file under the same rules as `--content-file`, feed it through stdin, and relay the returned output in the active member's voice. The delegate always works on a copy, including inside read/write grants and member clones, and its changes to regular files are written back to the repository only when the task ends successfully; review them with plain git before committing. Allow several minutes with a longer tool timeout or background execution, wait for completion, and relay any environment/login refusal as returned. A running GuildBotics server with the same workspace ID selected is required. It starts the command and its isolated environment. The CLI prints a run ID before submission; use `guildbotics run --resume <ID>` on the same device to retrieve the result and write back with the original conflict checks after the CLI stops. The CLI keeps exported file hashes per run ID until write-back succeeds or the run definitively fails. Git metadata, symlinks, and ignored new files are not written back; commit after write-back in the interactive session. Results expire after 24 hours, may be evicted earlier when the store is full, and are lost on server restart. This delegation runs from the host; do not invoke it inside an isolated agent environment. Use the repository root as cwd. Copying only a subdirectory can leave the original parent `.git` visible through a grant or clone mount, so committing during execution changes the original history.
- Before a command that writes free-form content, make a directory of your own with `mktemp -d`, outside the repository and worktree, and create a UTF-8 file in it. Write the exact content with a file-editing capability instead of an inline shell literal, pass the path as one argument to `--content-file`, and delete the directory even when the command fails. Never create or stage this file inside the repository.
- Use:

```bash
git add -A   # or: git add <paths> to stage only part of your changes
"$HOME/.guildbotics/bin/guildbotics" member git commit --person <person_id> --repo-path <current_repo_path> --content-file "<temporary_content_file>" --workspace-mode current
"$HOME/.guildbotics/bin/guildbotics" member git push --person <person_id> --repo-path <current_repo_path> --workspace-mode current
```

Run `member git commit` without `member git push` when the user asks for a commit without a push.

## Creating a ticket and starting it in the conversation

When the user explicitly asks to create a ticket and implement it in the same
interactive session, create it with `--interactive --assignee <active_person_id>
--lane in_progress --human-approved`. Use the roster's `person_id` for
`--assignee`; the CLI resolves the ticket provider's account, including human
members when the user names one. These initial settings are part of the user's
approval to create the ticket. Do not create it ready and then set interactive
mode or assignment separately: patrol could take the intermediate state.

対話中に「チケットを作成してそのまま実装」と指示されたら、作成時に
`--interactive --assignee <自分のperson_id> --lane in_progress --human-approved`
を指定する。ユーザーが別の担当を指定した場合は人を含む名簿の person_id を使う。
作成時の指定はチケット作成の承認に含まれる。

Pass `--parent <ticket_url>` and repeated `--blocked-by <ticket_url>` when
requested. Without `--lane`, the default is `ready` with a parent or blockers,
and `new` otherwise, with or without `--interactive`. An ordinary assigned
ticket in `ready` need not be interactive. Providers that do not support
interactive tickets refuse `--interactive` before creating anything.

When the user asks to work on an existing ticket interactively, read it before
starting work. The request approves the following changes. Closed tickets stay
unchanged; report that reopening is a human decision.

1. Check `assignees`. If empty, assign yourself; if only you, keep it. If anyone
   else is assigned (including a human or alongside you), ask before changing
   anything: setting interactive mode stops their patrol too. If the user agrees,
   replace all assignees with yourself; otherwise keep the assignment.
2. Set the ticket's own `interactive` flag if false, even with an
   `interactive_ancestor`, since the ancestor setting may be removed independently.
3. If any `blocked_by` ticket is open, keep the lane, omit `--lane`, and report
   that work cannot start until the blockers close. Otherwise move `new` or
   `ready` to `in_progress`; keep `in_progress`. Keep `done` and
   report that it is already in the completed lane.

Apply the decided changes in ONE `member ticket update --person <person_id>
--url <ticket_url> --interactive --assignee <person_id> --lane in_progress
--human-approved`, omitting unchanged fields. Separate writes could let patrol
start the ticket in an intermediate state. If the provider returns unsupported,
nothing changed: explain which change is unavailable and that patrol may still
work in parallel, then ask whether to continue interactively. Report the resulting
assignees, interactive flag, and lane. For other update failures, read the ticket
again and report its actual state: an error can follow partial writes. To return to automatic patrol on request,
use `--no-interactive --human-approved`; an `interactive_ancestor` means the
inherited interactive mode still applies and must be reported.

人が既存のチケットを対話で進めるよう依頼したら、着手前に読み取り、次をそろえる。
依頼をこの変更への承認として扱う。閉じたチケットは変更せず、開き直すかは人が決めると伝える。

1. `assignees` が空なら自分にする。自分だけならそのまま。
   人や自分と一緒の場合も含め、自分以外の担当がいれば、何も変える前にユーザーに確認する。
   対話指定だけでも担当者の巡回が止まるため。了承されたら自分だけにし、替えない場合は担当を保つ。
2. 自身の `interactive` が false なら付ける。`interactive_ancestor` からの継承だけでも付ける。
   祖先の指定はこのセッションと関係なく外されうる。
3. `blocked_by` に開いたチケットがあれば、レーンを保ち `--lane` を付けず、依存先が閉じるまで着手できないと伝える。
   それ以外は新規・着手可なら作業中へ動かす。作業中ならそのまま。完了なら動かさず、そのことを伝える。

決めた変更を1回の `ticket update --interactive --assignee <自分のperson_id>
--lane in_progress --human-approved` にまとめ、変更不要な項目は省く。
分けると巡回が途中の状態を拾いうる。provider が unsupported で断った場合は何も変わらない。
できない変更と巡回が並行して進めうることを伝え、このまま対話で進めるか確認する。
結果の担当・対話指定・レーンを伝える。unsupported 以外の失敗では、一部が書き込まれている可能性があるため、
チケットを読み直して実際の状態を伝える。自動巡回へ戻す依頼には
`--no-interactive --human-approved` を使い、`interactive_ancestor` による継承が残るなら伝える。

## Definition of Done

Before sending your final interactive reply, complete the standard work procedure from the member capabilities in the user's current repository: verification of code changes, the requested publishing steps (commit, push, PR, comments, replies, or reactions), and memory maintenance.
When the work created, reused, or updated an open PR, run `guildbotics run repository/pr_checks` after the final push and do not report completion unless its `readiness` is `ready`. Resolve its `completion_blockers`; `base_out_of_date` requires updating the head only when the host requires it. `base_conflict` means the head conflicts with the current base; merge the base into the head, resolve conflicts, publish, and check again. The checked head SHA must still be current. Closed and merged PRs return `readiness=not_applicable`, while their CI rollup, checks, and failed logs remain available for inspection. `rollup=no_checks` remains visible and is ready only when the current base also has no checks; checks on the base make an empty head a registration-pending blocker.
While you wait for CI in the turn, keep the `feedback` of the first `pr_checks` read and compare it on every repeated read. When it changes, stop waiting even if checks are still pending: read the new reviews and comments, answer them with the Handling review feedback procedure, publish any fix, and then wait again from the latest `feedback`.
Then write the final reply in the active member's voice.

## Interactive Memory Obligations

When the user asks the member to remember, correct, promote, or archive memory, do it through `guildbotics member memory ... --person <person_id>` instead of asking the user to edit files. Policy memory still requires the user's instruction or approval before using `--policy-approved`.

## Execution Mode Guardrail

If a prompt contains `guildbotics_execution_mode=workflow` or `guildbotics_execution_mode=delegated`, that prompt is the primary contract for the run.
Do not apply this skill's Workspace Rules or Definition of Done to that run.
