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
                "(--issue-url <url> [--pr-url <url>] | --pr-url <url> "
                "| --repo <owner/repo> --branch <name>)",
                "Clone/checkout an isolated member workspace: a ticket branch "
                "(--issue-url), a PR head (--pr-url, alone or together with "
                "--issue-url, which checks out the PR head), or an ad-hoc branch "
                "(--repo --branch). --repo cannot be combined with the URL options.",
            ),
            (
                "guildbotics member git commit --person <person> --repo-path <path> "
                "(--message-file <file> | --message-stdin) "
                "[--workspace-mode member|current]",
                "Commit already-staged changes with the member identity (git config untouched).",
            ),
            (
                "guildbotics member git push --person <person> --repo-path <path> "
                "[--workspace-mode member|current]",
                "Push the current branch with the member credential.",
            ),
            (
                "guildbotics member git publish --person <person> --repo-path <path> "
                "(--message-file <file> | --message-stdin) "
                "[--workspace-mode member|current]",
                "Commit already-staged changes with the member identity, then push.",
            ),
        ],
    ),
    (
        "GitHub",
        [
            (
                "guildbotics member github issue inspect --person <person> --url <issue_url>",
                "Read an issue and its comments.",
            ),
            (
                "guildbotics member github issue comment --person <person> --url <issue_url> --body-file <file>",
                "Comment on an issue in the member voice.",
            ),
            (
                "guildbotics member github issue create --person <person> --repo <owner/repo> "
                "--title-file <file> --body-file <file> [--add-to-project]",
                "Open a follow-up issue.",
            ),
            (
                "guildbotics member github pr inspect --person <person> --url <pr_url> "
                "[--include-comments] [--include-diff]",
                "Read a PR, optionally including review threads and diff comment coordinates.",
            ),
            (
                "guildbotics member github pr create --person <person> --repo <owner/repo> --head <branch> "
                "[--base <branch>] (--title-file <f> --body-file <f> | --content-stdin) "
                "[--issue-url <url>] [--draft auto|true|false]",
                "Open or reuse a PR.",
            ),
            (
                "guildbotics member github pr comment --person <person> --url <pr_url> --body-file <file>",
                "Comment on a PR conversation.",
            ),
            (
                "guildbotics member github pr review-comment --person <person> --url <pr_url> "
                "--path <file> --line <n> [--side LEFT|RIGHT] [--start-line <n> --start-side LEFT|RIGHT] "
                "--body-file <file>",
                "Create a new inline review comment on a PR diff line.",
            ),
            (
                "guildbotics member github pr reply --person <person> --url <pr_url> "
                "--reply-target-id <id> --body-file <file>",
                "Reply to an inline review thread.",
            ),
            (
                "guildbotics member github reaction add --person <person> --repo <owner/repo> "
                "--target issue-comment|pr-review-comment --comment-id <id> --reaction <reaction>",
                "React to an issue or review comment.",
            ),
        ],
    ),
    (
        "Chat (Slack)",
        [
            (
                "guildbotics member chat identity --person <person> --service slack",
                "Show the member's chat identity.",
            ),
            (
                "guildbotics member chat inspect thread --person <person> --service slack "
                "(--message-url <url> | --channel-id <id> --thread-ts <ts>) [--limit <n>]",
                "Read a thread before replying or reacting.",
            ),
            (
                "guildbotics member chat inspect channel --person <person> --service slack "
                "(--channel-id <id> | --channel-name <name>) [--oldest-ts <ts>] [--latest-ts <ts>] [--limit <n>]",
                "Read recent channel messages.",
            ),
            (
                "guildbotics member chat reply --person <person> --service slack "
                "(--message-url <url> | --channel-id <id> --thread-ts <ts>) "
                "(--body-file <file> | --body-stdin)",
                "Reply in a thread in the member voice.",
            ),
            (
                "guildbotics member chat post --person <person> --service slack "
                "(--channel-id <id> | --channel-name <name>) "
                "(--body-file <file> | --body-stdin)",
                "Post a new channel message.",
            ),
            (
                "guildbotics member chat reaction add --person <person> --service slack "
                "(--message-url <url> | --channel-id <id> --message-ts <ts>) "
                "--reaction ack|agree|celebrate|support",
                "Add a semantic reaction.",
            ),
        ],
    ),
    (
        "Memory",
        [
            (
                "guildbotics member memory record --person <person> --scope personal|team "
                "--title <title> (--body-file <file> | --body-stdin) "
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
                "Read one memory document's metadata, body, and asset paths without changing recency.",
            ),
            (
                "guildbotics member memory update --person <person> --id <doc-id> "
                "[--team] [--body-file <file> | --body-stdin] [--title <title>] "
                "[--summary <text>] [--add-keyword <word>] [--remove-keyword <word>] "
                "[--pin|--unpin] [--policy-approved] [--set <key=value> ...]",
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
                "guildbotics member chat noop --person <person> --run-id <id> --service slack "
                "--channel-id <id> --thread-ts <ts> --event-id <id> --reason-file <file>",
                "Record a deliberate no-op for a chat event.",
            ),
            (
                "guildbotics member chat complete --person <person> --run-id <id> --service slack "
                "--channel-id <id> --thread-ts <ts> --event-id <id> --status done|asking|blocked --summary-file <file>",
                "Finish a chat workflow run with evidence.",
            ),
            (
                "guildbotics member task complete --person <person> --run-id <id> --ticket-url <url> "
                "--status done|asking|blocked --summary-file <file>",
                "Finish a ticket workflow run with evidence.",
            ),
            (
                "guildbotics member task status --person <person> --run-id <id>",
                "Inspect recorded run evidence.",
            ),
        ],
    ),
]

_STANDARD_WORK_PROCEDURE: list[str] = [
    "Inspect first: read the current issue / PR / thread with the member inspect "
    "commands before acting. Fields owned by GitHub or Slack (state, assignees, "
    "labels, PR links, bodies, comments, review threads) are canonical in that "
    "inspect output.",
    "Edit, then run the relevant tests, linters, and checks before publishing any "
    "code change.",
    "Stage with plain git (`git add`), then commit and push through `member git "
    "commit`, `member git push`, or `member git publish`.",
    "When issue work changed code, open or reuse a PR with `member github pr "
    "create`. When creating new PR inline feedback, first inspect the PR with "
    "`member github pr inspect --include-diff`, then use `member github pr "
    "review-comment` with explicit diff coordinates from `files[].commentable_lines` "
    "(`path`, `line`, `side`, and optional `--start-line` / `--start-side`). When "
    "addressing existing PR review threads, reply with `member github pr reply` "
    "using the `reply_target_id` from `pr inspect --include-comments`.",
    "On completion, leave an externally visible trace at the place that "
    "corresponds to the work's entry point: a comment or status update on the "
    "originating issue or PR for issue-driven work, the review thread or PR "
    "conversation for PR review work, the Slack thread for Slack-driven work. "
    "Avoid duplicate posts and posts explicitly marked as unnecessary.",
    "Before finishing, maintain memory according to the rules below.",
]

_CROSS_CUTTING_RULES: list[str] = [
    "Publishing writes go through these `guildbotics member ...` commands: commits, pushes, PRs, "
    "issue/PR comments, and Slack posts/reactions. Never use gh, raw GitHub/Slack tokens or APIs, "
    "raw `git commit` / `git push`, or raw Slack HTTP calls for those.",
    "Local-only git is the opposite: run `git add` and `git switch -c` yourself as plain git. "
    "`member git commit` / `publish` commit only what you staged and apply the member name/email "
    "to that one commit without changing the repository git config.",
    "Apply `communication_style` by output kind: `interactive_replies` for interactive "
    "progress updates and final replies to the user; `github_comments` for issue comments, "
    "PR conversation comments, inline review comments, and review thread replies; "
    "`neutral_documents` for issue/PR "
    "titles and bodies, commit messages, and task summaries; `machine_outputs` for command "
    "output, command arguments, IDs, paths, workflow completion JSON, and workflow "
    "`AgentResponse.message`.",
    "`memory.pinned` from `member context` contains standing rules. `memory.digest` is only a hint "
    "that a relevant note may exist.",
    "Before work, recall prior memory by source whenever a ticket URL, PR URL, Slack thread URL, "
    "or thread timestamp is known. Use topic recall only when prior notes seem likely, with "
    "repeated `--query` options for synonyms and English/Japanese variants. Get only promising "
    "hits; if nothing looks relevant, do not get.",
    "Memory can carry prior context, rationale, and progress, but it is not the canonical current "
    "state of GitHub, Slack, or code. Reality-check every memory you read against the current "
    "owning system, and when sources differ prefer the owning system for canonical fields such "
    "as GitHub state, assignees, labels, PR links, Slack thread contents, and code behavior.",
    "When the requester asks what the member remembers, recorded, learned, or previously "
    "discussed, use memory as the primary basis for the answer, then verify freshness against "
    "the owning system when current state matters.",
    "Before finishing, touch memories that actually helped, update memories that reality proves "
    "wrong, and record only durable reusable lessons. Policy memory (`kind: policy`) requires "
    "human approval through `--policy-approved`; autonomous workflow runs must propose policy "
    "changes in their normal output instead of updating policy directly.",
    "After creating, reusing, or updating a PR, record durable PR work context with "
    "`member memory record --pr <pr_url>`, adding `--ticket <url>` and/or `--thread <url>` "
    "when known: include the branch, commit, verification result, what was completed, and "
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
    lines.append("### Rules")
    lines.extend(f"- {rule}" for rule in _CROSS_CUTTING_RULES)
    return "\n".join(lines).strip()
