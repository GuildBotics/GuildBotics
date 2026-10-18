"""Single source of truth for the GuildBotics member capability reference.

This describes *what a configured member can do and how*: the ``guildbotics
member ...`` command surface, the standard work procedure, and the
cross-cutting rules (including the memory and communication-style contracts).
Everything here is mode-independent: it applies whether the member is invoked
by a workflow or interactively through the skill.

What it deliberately excludes is a run's completion contract — the primary
objective, the required completion command, and status semantics. Those belong
to each entrypoint (``SKILL.md`` and the workflow prompts), never to this
shared reference, so that workflow-invoked and interactive behavior are never
blurred. The reverse also holds: guidance that would otherwise be duplicated
across entrypoints belongs here, not in the prompts.

Consumers render from this one source:
- ``member context`` embeds it (the mandatory first call in every entrypoint).
- ``member help`` prints it on demand.
- The ``member`` Click commands fill missing ``--help`` descriptions from the
  per-command summaries (see :func:`command_summary`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# (group title, [(command usage, one-line purpose), ...])
_CAPABILITY_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Member context",
        [
            (
                "guildbotics member context --person <person> [--check-credentials]",
                "Read role, profile, communication style, credentials, and this reference.",
            ),
            (
                "guildbotics member help",
                "Reprint this capability reference without re-reading the full context.",
            ),
        ],
    ),
    (
        "Git — the member commands only add the commit identity and credential; "
        "stage and branch with plain git yourself",
        [
            (
                "guildbotics member git prepare --person <person> "
                "(--issue-url <url> [--pr-url <url> | --repo <repository>] | --pr-url <url> "
                "| --repo <repository> --branch <name>)",
                "Clone/checkout an isolated member workspace: a ticket branch "
                "`ticket/<key>` (--issue-url, with --repo for a ticket that belongs to "
                "no repository), a PR head (--pr-url, alone or together with "
                "--issue-url, which checks out the PR head), or an ad-hoc branch "
                "(--repo --branch). A repository is named by its path. "
                "Member workspaces are worked on only inside a running GuildBotics "
                "command, whose isolated environment runs every git of them.",
            ),
            (
                "guildbotics member git commit --person <person> --repo-path <path> "
                "--content-file <file> "
                "[--workspace-mode member|current]",
                "Commit already-staged changes with the member identity (git config untouched).",
            ),
            (
                "guildbotics member git push --person <person> --repo-path <path> "
                "[--workspace-mode member|current]",
                "Push the current branch with the member credential and report "
                "readiness for matching open PRs.",
            ),
            (
                "guildbotics member git publish --person <person> --repo-path <path> "
                "--content-file <file> "
                "[--workspace-mode member|current]",
                "Commit already-staged changes with the member identity, then push "
                "and report readiness for matching open PRs.",
            ),
        ],
    ),
    (
        "Ticket",
        [
            (
                "guildbotics run repository/issue_inspect --person <person> url=<ticket_url>",
                "Read a ticket, its comments, its lane on the board, and the pull "
                "requests that refer to it.",
            ),
            (
                "guildbotics member ticket read --person <person> --resource <name> "
                "--subject <ticket_url> [--params <json>] [--continuation <value>]",
                "Read one bounded page of a ticket's resource: tickets (the ticket "
                "itself: key, tracker, repository, lane, ...), ticket_comments, or "
                "ticket_pull_requests (the code host's pull requests that refer to "
                "it). Collections accept page_size (1-100). Results contain "
                "normalized items and continuation; reuse identical conditions for the "
                "next page. HTTP methods, headers, and GraphQL are not accepted.",
            ),
            (
                "guildbotics member ticket comment --person <person> --url <ticket_url> --content-file <file>",
                "Comment on a ticket in the member voice.",
            ),
            (
                "guildbotics member ticket create --person <person> --tracker <tracker> "
                "--title <title> --content-file <file> [--label <name> ...] "
                "[--add-to-board|--no-add-to-board] --human-approved",
                "Open a follow-up ticket a human asked for, in the tracker a ticket's "
                "read names (`tracker`).",
            ),
            (
                "guildbotics member ticket update --person <person> --url <ticket_url> "
                "[--content-file <file>] [--title <title>] [--add-label <name> ...] "
                "[--remove-label <name> ...] "
                "[--state open|closed [--state-reason completed|not_planned] --human-approved]",
                "Change a ticket's body, title, labels, or state; empty content "
                "removes the body.",
            ),
            (
                "guildbotics member ticket reaction add --person <person> --url <ticket_url> "
                "--comment-id <id> --reaction ack|agree|celebrate|support",
                "React to a comment of the ticket, by the id its read gave it.",
            ),
        ],
    ),
    (
        "Pull request",
        [
            (
                "guildbotics run repository/pr_inspect --person <person> url=<pr_url> "
                "[include_comments=true] [include_diff=true]",
                "Read a PR, optionally including conversation comments, review summaries, "
                "review threads, and diff comment coordinates.",
            ),
            (
                "guildbotics run repository/pr_checks --person <person> url=<pr_url> "
                "[failed_logs=true] [log_tail_bytes=<n>]",
                "Read a PR head's CI rollup, base freshness, completion readiness, "
                "and optional failed CI job log tails (65536 bytes per job by default, "
                "reduced when jobs share the serialized page budget).",
            ),
            (
                "guildbotics member pr read --person <person> --resource <name> "
                "--subject <pr_url> [--params <json>] [--continuation <value>]",
                "Read one bounded page of a PR's resource: pull_requests, "
                "pull_request_comments (conversation comments), pull_request_reviews, "
                "pull_request_files, pull_request_threads, review_thread_comments, or "
                "pull_request_readiness. Collections accept page_size (1-100); "
                "review_thread_comments also requires node from pull_request_threads. "
                "Readiness accepts failed_logs (boolean) and log_tail_bytes "
                "(1-65536, default 65536). "
                "Results contain normalized items and continuation; reuse identical "
                "conditions for the next page. HTTP methods, headers, and GraphQL "
                "are not accepted.",
            ),
            (
                "guildbotics member pr create --person <person> --repo <repository> --head <branch> "
                "[--base <branch>] --title <title> --content-file <file> "
                "[--issue-url <url> ... [--closes-issue|--refs-issue]] [--draft true|false]",
                "Open a PR, or return the existing open PR for the same head and base branches. "
                "--issue-url, which may be repeated, links each ticket in the PR body as a "
                "reference, or with --closes-issue so that merging the PR closes every one "
                "of them; a code host that cannot close one of those tickets on a merge "
                "refuses --closes-issue.",
            ),
            (
                "guildbotics member pr update --person <person> --url <pr_url> "
                "[--content-file <file>] [--title <title>] [--drop-issue-links]",
                "Change a PR's body or title. Body replacement preserves the ticket "
                "links the existing body carries as the code host reads them; new "
                "links to the same ticket take precedence. Empty content keeps only "
                "the links. --drop-issue-links requires content and disables "
                "preservation.",
            ),
            (
                "guildbotics member pr comment --person <person> --url <pr_url> --content-file <file>",
                "Comment on a PR conversation.",
            ),
            (
                "guildbotics member pr review --person <person> --url <pr_url> "
                "--event approve|request_changes|comment --head <sha> --content-file <file>",
                "Submit a review verdict as a review on the head the member read "
                "(`head_sha` of the PR's read); a conversation comment does not consume "
                "a review request or make the member a reviewer. When the head has "
                "moved, nothing is written: read the new commits, then submit again on "
                "the new head. A code host without request_changes refuses it.",
            ),
            (
                "guildbotics member pr review-comment --person <person> --url <pr_url> "
                "--head <sha> --path <file> --line <n> [--side LEFT|RIGHT] "
                "[--start-line <n> --start-side LEFT|RIGHT] --content-file <file>",
                "Create a new inline review comment on a diff line of the head the "
                "member read; when the head has moved, read the new commits and "
                "comment again.",
            ),
            (
                "guildbotics member pr reply --person <person> --url <pr_url> "
                "--reply-target-id <id> --content-file <file>",
                "Reply to an inline review thread.",
            ),
            (
                "guildbotics member pr reaction add --person <person> --url <pr_url> "
                "--comment-id <id> --reaction ack|agree|celebrate|support",
                "React to a conversation comment, a review thread comment, or a "
                "review's body of the PR, by the id its read gave it.",
            ),
            (
                "guildbotics member pr artifact download --person <person> "
                "--url <pr_url> --name <artifact> [--dest <dir>]",
                "Download and extract a size-limited CI artifact of the PR's head; "
                "remove repository files after inspection.",
            ),
        ],
    ),
    (
        "Repository information",
        [
            (
                "guildbotics member repo-info read --person <person> --resource "
                "dependency_alerts --subject <repository> [--params <json>] "
                "[--continuation <value>]",
                "Read one bounded page of the repository's dependency_alerts, the "
                "repository named by its path. It accepts state (open by default, "
                "resolved, dismissed) and page_size (1-100, default 30). With an id "
                "condition, it returns that one alert and accepts no other condition "
                "or continuation. For formatted or JSON alerts, run "
                "repository/security_alerts in a ready command environment. "
                "Results contain normalized items and continuation; reuse identical "
                "conditions for the next page. HTTP methods, headers, and GraphQL "
                "are not accepted.",
            ),
        ],
    ),
    (
        "Chat",
        [
            (
                "guildbotics member chat updates --person <person>",
                "Check the source thread's durable queue before replying or publishing in a chat workflow; "
                "reconsider new messages, and stop external writes when reception is unavailable.",
            ),
            (
                "guildbotics member chat identity --person <person>",
                "Show the member's chat identity.",
            ),
            (
                "guildbotics member chat inspect thread --person <person> "
                "(--message-url <url> | --channel-id <id> --thread-id <id>) [--limit <n>]",
                "Read a thread before replying or reacting.",
            ),
            (
                "guildbotics member chat inspect channel --person <person> "
                "(--channel-id <id> | --channel-name <name>) [--since <iso-time>] [--until <iso-time>] [--limit <n>]",
                "Read recent channel messages.",
            ),
            (
                "guildbotics member chat resolve-channel --person <person> "
                "--channel-name <name>",
                "Resolve a channel name to its channel id without reading or posting; "
                "the id is empty when no channel has that name.",
            ),
            (
                "guildbotics member chat reply --person <person> "
                "(--message-url <url> | (--channel-id <id> | --channel-name <name>) "
                "--thread-id <id>) "
                "--content-file <file>",
                "Reply in a thread in the member voice.",
            ),
            (
                "guildbotics member chat post --person <person> "
                "(--channel-id <id> | --channel-name <name>) "
                "--content-file <file>",
                "Post a new channel message.",
            ),
            (
                "guildbotics member chat reaction add --person <person> "
                "(--message-url <url> | --channel-id <id> --message-id <id>) "
                "--reaction ack|agree|celebrate|support",
                "Add a semantic reaction.",
            ),
        ],
    ),
    (
        "Memory",
        [
            (
                "guildbotics member memory record --person <person> [--scope personal|team] "
                "--title <title> --content-file <file> "
                "[--summary <text>] [--keyword <word> ...] [--ticket <url>] [--pr <url>] "
                "[--channel <url>] [--thread <url>] [--kind note|policy] [--pin] "
                "[--policy-approved] [--set <key=value> ...]",
                "Create a memory document and move it to the front of the digest.",
            ),
            (
                "guildbotics member memory recall --person <person> --query <text> "
                "[--query <text> ...] [--meta-only] [--limit <n>]",
                "Search personal and team memory by literal OR queries and return compact hits.",
            ),
            (
                "guildbotics member memory get --person <person> --id <doc-id> [--team]",
                "Read one memory document's metadata and body without changing recency.",
            ),
            (
                "guildbotics member memory update --person <person> --id <doc-id> "
                "[--team] [--content-file <file>] [--title <title>] "
                "[--summary <text>] [--keyword <word> ...] [--add-keyword <word> ...] "
                "[--remove-keyword <word> ...] [--ticket <url>] [--pr <url>] "
                "[--channel <url>] [--thread <url>] [--pin|--unpin] "
                "[--kind note|policy] [--policy-approved] [--set <key=value> ...]",
                "Replace selected body or metadata fields and move the document to the digest front.",
            ),
            (
                "guildbotics member memory touch --person <person> --id <doc-id> [--team]",
                "Mark a useful memory as actually used by moving it to the digest front.",
            ),
            (
                "guildbotics member memory archive --person <person> --id <doc-id> "
                "[--team] [--policy-approved]",
                "Move a stale memory under archived/ and remove it from recall and digest.",
            ),
            (
                "guildbotics member memory promote --person <person> --id <doc-id>",
                "Move a personal memory into team memory without changing the document id.",
            ),
        ],
    ),
    (
        "Run completion — workflow runs only",
        [
            (
                "guildbotics member chat noop --person <person> --content-file <file>",
                "Record a deliberate no-op for the chat run's event.",
            ),
            (
                "guildbotics member chat complete --person <person> "
                "--status done|asking|blocked --content-file <file>",
                "Finish the chat workflow run on its event with evidence.",
            ),
            (
                "guildbotics member task complete --person <person> "
                "--status done|asking|blocked|awaiting_checks --content-file <file>",
                "Finish the ticket workflow run on its ticket with evidence, "
                "revalidating affected PR readiness before accepting done, or end "
                "it to await the PRs' pending checks, which resume it.",
            ),
            (
                "guildbotics member task status --person <person>",
                "Inspect the ticket run's recorded evidence.",
            ),
        ],
    ),
]

_STANDARD_WORK_PROCEDURE: list[str] = [
    "Run bundled inspections on the host with `guildbotics run repository/issue_inspect "
    "--person <person> url=<ticket_url>` (or repository/pr_inspect, "
    "repository/pr_checks with url=<pr_url>). Inside a workflow or delegated command's "
    "isolated environment, use `/opt/guildbotics/venv/bin/python -m guildbotics.guest.entry "
    "repository/issue_inspect url=<ticket_url>` instead: it runs inside "
    "the existing environment, inherits the main command's access contract and "
    "member, and uses its member broker. Do not start a host run from there. "
    "Commands return JSON. A failed or incomplete read is not an empty result.",
    "Inspect first: read the current ticket / PR / thread with the bundled repository inspections or member chat inspect "
    "commands before acting. Fields owned by the code host or the chat (state, assignees, "
    "labels, PR links, bodies, comments, review threads) are canonical in that "
    "inspect output.",
    "Ground repository judgments in the repository's own guidelines: before "
    "drafting or creating a ticket for a repository, or settling a design or "
    "implementation policy for one, read its agent and contributor guidelines "
    "(AGENTS.md, CLAUDE.md, CONTRIBUTING, or equivalents) and align the outcome "
    "with them. The version on the origin default branch is canonical: with an "
    "existing checkout, run `git fetch origin` and read `git show "
    "origin/<default-branch>:<file>` instead of trusting possibly stale "
    "working-tree files; without one, create a checkout first with `member git "
    "prepare` (its output reports the default branch).",
    "When addressing PR review feedback, follow the Handling review feedback "
    "procedure below before editing.",
    "Edit, then run the relevant tests, linters, and checks before publishing any "
    'code change. When one fails, never stop at "unrelated to this change": '
    "identify the cause, write the issue draft it deserves, and hand that draft to "
    "the requester for a decision instead of registering it or fixing it yourself.",
    "Inside a workflow or delegated command's isolated environment, the turn ends "
    "with your final answer, and whatever still runs in the environment stops with "
    "it: the completion notice of a background job never arrives. Run a long "
    "verification command (tests, builds, linters) in the foreground with a timeout "
    "long enough for it to finish, or start it in the background and check for its "
    "completion marker at short intervals until it appears. Never wait a fixed time "
    "for it, and never end the turn while it still runs. This covers only the "
    "commands you run inside the environment; CI runs outside it and is covered "
    "below.",
    "Stage with plain git (`git add`), then commit and push through `member git "
    "commit`, `member git push`, or `member git publish`.",
    "When ticket work changed code, open or reuse a PR with `member pr "
    "create`. Pass `--issue-url` once for each ticket the PR works on, and add "
    "`--closes-issue` only when every one of those tickets is complete once this PR "
    "merges into the branch that receives its work. `--closes-issue` applies to all "
    "of them, so with it link only the tickets the merge completes and refer to any "
    "other on a standalone line in the body. "
    "A child ticket can therefore be closed by a PR to its parent's feature "
    "branch: pass the child with `--issue-url <child> --closes-issue` and refer to "
    "the parent ticket on a standalone line in the body. A PR from that feature "
    "branch to the default branch closes the parent ticket. Keep the default "
    "reference for stacked PRs, partial steps, and tracking or parent tickets "
    "that the merge does not complete. A merge into a branch other than the default "
    "branch closes no ticket automatically, so after a human merges a child "
    "PR into a feature branch, follow the manual-close rule below. A closing link "
    "only states intent and does not by itself authorize closing the ticket.",
    "When creating new PR inline feedback, first inspect the PR with "
    "`repository/pr_inspect include_diff=true`, then use `member pr "
    "review-comment --head <head_sha>` with explicit diff coordinates from "
    "`files[].commentable_lines` (`line`, `side`, and optional `--start-line` / "
    "`--start-side`); take `path` from the containing file and `head_sha` from the "
    "same read.",
    "When addressing existing PR review threads, reply with `member pr reply` "
    "using the `reply_target_id` from `repository/pr_inspect include_comments=true`.",
    "After opening or updating a PR, inspect its CI and completion readiness with "
    "`repository/pr_checks`. "
    "If a check fails, use `repository/pr_checks failed_logs=true` to "
    "identify the cause, fix failures caused by the change, publish the fix, and "
    "check CI again. Do not report the work complete unless `readiness` is `ready`: "
    "for every open PR, every observed check must succeed, the head must not be behind "
    "the current base, and the checked head SHA must still be current. Closed and merged "
    "PRs are not completion targets, but their CI rollup, checks, and failed logs remain "
    "available for inspection. `rollup=no_checks` is visible and does not block a "
    "repository whose base also has no checks; if the base has checks, an empty head is "
    "treated as registration pending. If an unrelated failure or an unavailable "
    "check blocks completion, follow the failure-handling rule above and report that blocker. "
    "In a ticket workflow run, do not wait for CI in the turn: when pending checks "
    "(`checks_pending`) are the only blocker left, finish the run with `member task "
    "complete --status awaiting_checks`, and the patrol resumes the work once the "
    "checks finish or the head moves.",
    "On completion, leave an externally visible trace at the place that "
    "corresponds to the work's entry point: a comment or status update on the "
    "originating ticket or PR for ticket-driven work, the review thread or PR "
    "conversation for PR review work, the chat thread for chat-driven work. "
    "Avoid duplicate posts and posts explicitly marked as unnecessary.",
    "Before finishing, maintain memory according to the rules below.",
]

_REVIEW_FEEDBACK_PROCEDURE: dict[str, list[str]] = {
    "Handling review feedback": [
        "List every finding, including earlier advice and reviews, not only the latest review.",
        "Group findings by mechanism or state rather than location. Check whether the same "
        "mechanism or state has received a second finding; if it has, question whether that "
        "mechanism is needed at all before repairing it. Use repository searches to count "
        "all locations with the same shape.",
        "Choose the repair before editing. If it adds an exit, check, exception, or fallback, first "
        "consider a design in which the problematic state cannot exist.",
        "Write down the implicit guarantees the change could remove: other roles played by "
        "moved code, removed mutual exclusion, or reordered calls.",
        "Get two independent inspections: after step 2 to find other holes with the same "
        "shape, and before committing to find guarantees implicitly removed by the diff. "
        "For each, use a fresh delegate (such as a subagent) without the author's conversation "
        "context; select a higher-capability model if the CLI supports model selection. "
        "Include decided matters with their reasons, and ask for file:line and a failing "
        "scenario for each finding. The delegate finds omissions; the author verifies each "
        "finding and decides whether to accept it. Record reasons for rejected findings "
        "on the PR: reply in the relevant thread when one exists, otherwise leave a "
        "PR conversation comment.",
        "Pin the whole population with tests, not just the reported locations. Deliberately "
        "break the repaired behavior and confirm that the tests fail.",
        "Reply to each thread with the cause's shape, the repair's shape, and the tests "
        "that pin it.",
    ],
    "レビュー指摘への対応": [
        "指摘を全部並べる。今回のレビューだけでなく、先行する助言やレビューが名指ししたものも含める。",
        "場所ではなく仕組みや状態で分ける。同じ仕組みや同じ状態に 2 件目の指摘が来ていないかを確かめ、"
        "来ていたら直す前にその仕組み自体が要るのかを問う。同じ形の箇所がほかにないかを検索で数える。",
        "直す形を編集前に決める。修正が「出口・検査・例外・フォールバックを足す」形なら、"
        "先にその状態が存在しなくて済む設計を検討する。",
        "変更で外れる暗黙の保証を書き出す。移した処理、外した排他、"
        "組み替えた呼び出し順が兼ねていた役割を見る。",
        "独立した見直しを 2 回受ける。手順 2 のあとには同じ形の穴を、コミットの前には差分が暗黙に外した"
        "保証を探す。それぞれ作者の会話の文脈を持たない新しい委任先 (サブエージェントなど) へ依頼し、"
        "CLI がモデルを選べるなら上位のモデルを指定する。依頼には決定済み事項を理由と一緒に含め、"
        "各指摘に file:line と壊れるシナリオを求める。委任先は見落としを探し、作者は指摘を 1 件ずつ"
        "確かめて採否を決める。採らなかったものは理由を PR に残す。該当スレッドがあれば返信し、"
        "なければ PR の会話コメントに書く。",
        "個別の箇所ではなく母集団をテストで固定する。直した振る舞いをわざと壊し、テストが落ちることを確かめる。",
        "スレッドごとに「原因の形」「直した形」「固定したテスト」を書いて返信する。",
    ],
}

_CROSS_CUTTING_RULES: list[str] = [
    "Ticket and pull request inspections use the bundled repository commands. All "
    "underlying ticket, code-host, and chat access, reads and writes alike, goes through the "
    "corresponding `guildbotics member ...` commands. Never use a service's own CLI "
    "such as `gh`, raw service tokens or APIs, or raw HTTP calls to those services. "
    "When a needed read "
    "has no member command, ask a human for the information instead of using a "
    "service's CLI or a raw API. Publishing git commits and pushes also goes through member "
    "commands; never use raw `git commit` or `git push`.",
    "Opening a ticket and closing or reopening one stay human decisions. Pass "
    "`--human-approved` only when a human in the originating conversation asked for or "
    "approved that specific ticket, never on the member's own judgment and never because "
    "another member asked for it. The one exception is a human's merge outside the default "
    "branch: if the code host does not close a ticket after a human merges its PR there, inspect "
    "the current PR and ticket and verify that the merge completed the ticket's whole scope. "
    "If it did, post a completion comment and close the ticket with `ticket update "
    "--state closed --state-reason completed --human-approved`. If it did not, leave "
    "the ticket open and comment on what remains.",
    "Labels come from the labels the target tracker already defines; `ticket create "
    "--label` and `ticket update --add-label` reject anything else. When the defined "
    "labels do not cover what the ticket needs, propose the new label to a human instead "
    "of inventing one or writing the distinction into the body.",
    "Priority is a human triage decision, so the member proposes it and never records it: "
    "do not express urgency by adding a label or setting a board or ticket field for it. "
    "Say what the member thinks the priority should be, and why, in the ticket comment, PR "
    "comment, or thread where the work was requested, and leave the recording to a human.",
    "What the inspect commands read maps to writes like this: `ticket update` writes body, "
    "title, labels, and state, and `pr update` writes body and title. Assignees and the "
    "lane are read-only on purpose, because who takes a ticket is assigned by a human on "
    "the board; PR state, draft, and base are read-only for the same reason (the "
    "patrol itself makes a PR a draft only at the automatic re-review limit, and only a "
    "human marks it ready for review again).",
    "Pass every free-form write body, message, reason, or run summary through "
    "`--content-file <file>`. Create a dedicated directory exclusively, outside the "
    "repository and worktree, with `mktemp -d` (on Windows, a new randomly named directory "
    "under your user's temporary directory), and create the UTF-8 file inside it. Write the "
    "exact content with a file-editing capability instead of an inline shell literal, pass the "
    "path as one argv value, and delete the directory with the file even when the command "
    "fails. Titles are single-line `--title` values and never belong in the content file.",
    "Files the user hands over from the Desktop are read-only copies GuildBotics keeps for "
    "the app session, so never leave a result beside them. Unless the request names a "
    "destination, put what you produce for the user -- a converted document, a resized "
    "image, anything that does not belong in the repository you are working in -- under "
    "`~/Documents/GuildBotics/`.",
    "Git work that publishes nothing is the opposite: run `git add` and `git switch -c` "
    "yourself as plain git. "
    "`member git commit` / `publish` commit only what you staged and apply the member name/email "
    "to that one commit without changing the repository git config.",
    "Apply `communication_style` by output kind: `interactive_replies` for interactive "
    "progress updates and final replies to the user; `comments` for ticket comments, "
    "PR conversation comments, inline review comments, and review thread replies; "
    "`neutral_documents` for ticket/PR "
    "titles and bodies, commit messages, and task summaries; `machine_outputs` for command "
    "output, command arguments, IDs, paths, workflow completion JSON, and workflow "
    "`AgentResponse.message`.",
    "`memory.pinned` from `member context` contains standing rules. `memory.digest` is only a hint "
    "that a relevant note may exist.",
    "Before work, recall prior memory by source whenever a ticket URL, PR URL, chat thread URL, "
    "or thread id is known. Use topic recall only when prior notes seem likely, with "
    "repeated `--query` options for synonyms and English/Japanese variants. Get only promising "
    "hits; if nothing looks relevant, do not get.",
    "Memory can carry prior context, rationale, and progress, but it is not the canonical current "
    "state of the code host, the chat, or code. Reality-check every memory you read against the current "
    "owning system, and when sources differ prefer the owning system for canonical fields such "
    "as ticket and PR state, assignees, labels, PR links, chat thread contents, and code behavior.",
    "When the requester asks what the member remembers, recorded, learned, or previously "
    "discussed, use memory as the primary basis for the answer, then verify freshness against "
    "the owning system when current state matters.",
    "Before finishing, touch memories that actually helped, update memories that reality proves "
    "wrong, and record only durable reusable lessons. Policy memory (`kind: policy`) requires "
    "human approval through `--policy-approved`; autonomous workflow runs must propose policy "
    "changes in their normal output instead of updating policy directly.",
    "After creating, reusing, or updating a PR, record durable PR work context with "
    "`member memory record --scope team --pr <pr_url>`, adding `--ticket <url>` "
    "and/or `--thread <url>` when known: include the branch, commit, verification result, "
    "what was completed, and "
    "remaining follow-up. Record reusable technical lessons as separate memory documents "
    "only when they are valuable beyond that one PR.",
    "Never display, infer, store, or copy secrets or token values.",
]


_SUBCOMMAND_NAME = re.compile(r"[a-z][a-z0-9-]*")


def _usage_command_path(usage: str) -> str:
    """Subcommand path (e.g. ``chat inspect thread``) of a catalog usage line."""
    names: list[str] = []
    for token in usage.removeprefix("guildbotics member").split():
        if not _SUBCOMMAND_NAME.fullmatch(token):
            break
        names.append(token)
    return " ".join(names)


_COMMAND_SUMMARIES: dict[str, str] = {
    _usage_command_path(usage): purpose
    for _, commands in _CAPABILITY_GROUPS
    for usage, purpose in commands
    if usage.startswith("guildbotics member ")
}


def command_summaries() -> Mapping[str, str]:
    """One-line purposes of the ``member`` subcommands, keyed by command path."""
    return _COMMAND_SUMMARIES


def command_summary(command_path: str) -> str:
    """Return the catalog's one-line purpose of a ``member`` subcommand.

    Raises ``KeyError`` for paths without a catalog entry, so a CLI command
    that is missing from ``_CAPABILITY_GROUPS`` fails fast at import time.
    """
    try:
        return _COMMAND_SUMMARIES[command_path]
    except KeyError:
        raise KeyError(
            f"'guildbotics member {command_path}' has no entry in the member "
            "capability catalog (_CAPABILITY_GROUPS)."
        ) from None


def capability_reference_text() -> str:
    """Render the curated member capability reference as markdown text."""
    lines: list[str] = []
    for title, commands in _CAPABILITY_GROUPS:
        lines.append(f"### {title}")
        for usage, purpose in commands:
            lines.append(f"- `{usage}` — {purpose}")
        lines.append("")
    lines.append("### Standard work procedure")
    lines.extend(f"- {step}" for step in _STANDARD_WORK_PROCEDURE)
    lines.append("")
    for title, steps in _REVIEW_FEEDBACK_PROCEDURE.items():
        lines.append(f"#### {title}")
        lines.append("")
        lines.extend(f"{number}. {step}" for number, step in enumerate(steps, start=1))
        lines.append("")
    lines.append("### Rules")
    lines.extend(f"- {rule}" for rule in _CROSS_CUTTING_RULES)
    return "\n".join(lines).strip()
