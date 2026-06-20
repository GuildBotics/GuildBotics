"""Single source of truth for the GuildBotics member capability reference.

This describes *what a configured member can do and how*: the ``guildbotics
member ...`` command surface plus the cross-cutting safety rules. It is
deliberately free of task-contract content (a run's primary objective, its
required completion command, status semantics). Those belong to each entrypoint
(``SKILL.md`` and the workflow prompts), never to this shared reference, so that
workflow-invoked and interactive behavior are never blurred.

Consumers render from this one source:
- ``member context`` embeds it (the mandatory first call in every entrypoint).
- ``member help`` prints it on demand.
"""

from __future__ import annotations

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
                "guildbotics member git prepare --person <person> --issue-url <url> [--pr-url <url>]",
                "Clone/checkout an isolated member workspace for the ticket or PR head.",
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
                "guildbotics member github pr inspect --person <person> --url <pr_url> [--include-comments]",
                "Read a PR and its review threads.",
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
                "[--channel <url>] [--thread <url>] [--kind note|policy] [--pin]",
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
                "[--pin|--unpin]",
                "Replace selected body or metadata fields and move the document to the digest front.",
            ),
            (
                "guildbotics member memory touch --person <person> --id <doc-id> [--team]",
                "Mark a useful memory as actually used by moving it to the digest front.",
            ),
            (
                "guildbotics member memory archive --person <person> --id <doc-id> [--team]",
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

_CROSS_CUTTING_RULES: list[str] = [
    "Publishing writes go through these `guildbotics member ...` commands: commits, pushes, PRs, "
    "issue/PR comments, and Slack posts/reactions. Never use gh, raw GitHub/Slack tokens or APIs, "
    "raw `git commit` / `git push`, or raw Slack HTTP calls for those.",
    "Local-only git is the opposite: run `git add` and `git switch -c` yourself as plain git. "
    "`member git commit` / `publish` commit only what you staged and apply the member name/email "
    "to that one commit without changing the repository git config.",
    "Apply the member voice from `communication_style` to conversational and document outputs; "
    "keep IDs, paths, command arguments, and machine-readable output factual.",
    "Memory recall and get are read-only. Use touch only after a memory actually helped, update it "
    "when reality proves it wrong, and record only durable reusable lessons. Policy memory "
    "(`kind: policy`) requires human approval through `--policy-approved` and cannot be changed "
    "from autonomous workflow runs.",
    "Never display, infer, store, or copy secrets or token values.",
]


def capability_reference_text() -> str:
    """Render the curated member capability reference as markdown text."""
    lines: list[str] = []
    for title, commands in _CAPABILITY_GROUPS:
        lines.append(f"### {title}")
        for usage, purpose in commands:
            lines.append(f"- `{usage}` — {purpose}")
        lines.append("")
    lines.append("### Rules")
    lines.extend(f"- {rule}" for rule in _CROSS_CUTTING_RULES)
    return "\n".join(lines).strip()
