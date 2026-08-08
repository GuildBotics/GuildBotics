#!/usr/bin/env python3
"""Reconcile a single GitHub tracking issue with a recomputed list of items.

Both dependency digests report a list that is rebuilt from scratch on every run,
so they need the same three transitions: open the issue when the list is not
empty, refresh it while the list stays not empty, and close it once the list
empties.

The issue is found by a marker in its body rather than by a label, because
label creation is a human decision in this repository. The marker also carries
the item ids of the previous run, which is how an item that appeared since the
last report is told apart from one that was already reported.

I/O goes through the ``gh`` CLI, which is preinstalled on GitHub runners and
supplies authentication and pagination.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any


def gh(args: Sequence[str], stdin: str | None = None) -> str:
    """Run a gh subcommand and return its stdout.

    Raises:
        RuntimeError: The command failed. gh reports API errors such as a
            missing token permission only on stderr, so the message carries it;
            a bare CalledProcessError would hide the reason for the failure.
    """
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`gh {' '.join(args)}` failed with status {result.returncode}: "
            f"{result.stderr.strip() or '(no stderr)'}"
        )
    return result.stdout


def build_marker(prefix: str, item_ids: Sequence[str]) -> str:
    """Render the hidden marker that identifies the issue and its reported ids."""
    return f"{prefix} ids={','.join(item_ids)} -->"


def extract_item_ids(body: str, prefix: str) -> set[str]:
    """Return the item ids recorded in a previously generated issue body."""
    match = re.search(rf"{re.escape(prefix)} ids=([^\s]*) -->", body)
    if match is None or not match.group(1):
        return set()
    return set(match.group(1).split(","))


def find_issue(repo: str, prefix: str) -> dict[str, Any] | None:
    """Return the open tracking issue carrying the given marker, if any."""
    raw = gh(
        [
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,body",
        ]
    )
    for issue in json.loads(raw):
        if prefix in (issue.get("body") or ""):
            return issue
    return None


def common_parser(description: str) -> argparse.ArgumentParser:
    """Build the argument parser shared by every digest entry point."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="Target repository as owner/name (default: $GITHUB_REPOSITORY).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rendered digest and the planned action without writing.",
    )
    return parser


def reconcile(
    repo: str,
    *,
    prefix: str,
    title: str,
    body: str,
    item_ids: Sequence[str],
    close_comment: str,
    on_new_items: Callable[[set[str]], str] | None = None,
    dry_run: bool = False,
) -> int:
    """Create, refresh, or close the tracking issue for the reported items.

    Args:
        repo: Target repository as ``owner/name``.
        prefix: Marker prefix identifying this digest's issue.
        title: Issue title, kept stable so notifications stay on one thread.
        body: Rendered issue body, without the marker line.
        item_ids: Ids of the reported items; an empty sequence closes the issue.
        close_comment: Comment posted when the issue is closed.
        on_new_items: Renders a comment for ids not present in the previous run.
        dry_run: Print the planned action instead of writing.

    Returns:
        A process exit code.
    """
    issue = find_issue(repo, prefix)
    number = issue["number"] if issue else None
    print(f"items: {len(item_ids)}; tracking issue: {number or 'none'}")

    if not item_ids:
        if issue is None:
            print("nothing to do: no items and no tracking issue.")
            return 0
        print(f"action: close issue #{number}")
        if not dry_run:
            gh(
                [
                    "issue",
                    "close",
                    str(number),
                    "--repo",
                    repo,
                    "--reason",
                    "completed",
                    "--comment",
                    close_comment,
                ]
            )
        return 0

    full_body = f"{build_marker(prefix, item_ids)}\n\n{body}"
    if dry_run:
        print("--- issue body ---")
        print(full_body, end="")
        print("--- end ---")

    if issue is None:
        print("action: create issue")
        if not dry_run:
            url = gh(
                [
                    "issue",
                    "create",
                    "--repo",
                    repo,
                    "--title",
                    title,
                    "--body-file",
                    "-",
                ],
                stdin=full_body,
            ).strip()
            print(f"created: {url}")
        return 0

    new_ids = set(item_ids) - extract_item_ids(issue.get("body") or "", prefix)
    print(f"action: update issue #{number}; new items: {len(new_ids)}")
    if dry_run:
        return 0

    gh(
        ["issue", "edit", str(number), "--repo", repo, "--body-file", "-"],
        stdin=full_body,
    )
    if new_ids and on_new_items is not None:
        gh(
            ["issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
            stdin=on_new_items(new_ids),
        )
    return 0
