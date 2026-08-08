#!/usr/bin/env python3
"""Mirror open Dependabot alerts into a single tracking issue.

Dependabot security updates (the automatic fix PRs) are deliberately disabled
for this repository: a bare version bump without the accompanying test and
behaviour fixes is not a reviewable change. This script instead reflects the
open alerts into one tracking issue, so the work enters the normal ticket flow.

The issue is identified by ``MARKER_PREFIX`` in its body rather than by a label,
because label creation is a human decision in this repository. The same marker
carries the alert numbers of the previous run, which is how a newly appeared
alert is told apart from one that was already reported.

Reads and writes go through the ``gh`` CLI, which is preinstalled on GitHub
runners and supplies authentication and pagination.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

MARKER_PREFIX = "<!-- dependabot-alert-digest"
MARKER_PATTERN = re.compile(rf"{re.escape(MARKER_PREFIX)} alerts=([\d,]*) -->")
ISSUE_TITLE = "Dependabot の未解決脆弱性アラート"
WORKFLOW_PATH = ".github/workflows/dependabot-alert-digest.yml"

SEVERITIES = ("critical", "high", "medium", "low")
UNKNOWN_SEVERITY_RANK = len(SEVERITIES)


def severity_rank(severity: str) -> int:
    """Return the sort rank of a severity, placing unknown values last."""
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return UNKNOWN_SEVERITY_RANK


def sort_alerts(alerts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order alerts by severity, then package name, then alert number."""
    return sorted(
        alerts,
        key=lambda alert: (
            severity_rank(alert["security_vulnerability"]["severity"]),
            alert["dependency"]["package"]["name"].lower(),
            alert["number"],
        ),
    )


def extract_alert_numbers(body: str) -> set[int]:
    """Return the alert numbers recorded in a previously generated issue body."""
    match = MARKER_PATTERN.search(body)
    if match is None or not match.group(1):
        return set()
    return {int(number) for number in match.group(1).split(",")}


def _cell(value: str | None) -> str:
    """Render a table cell as inline code, or as an em dash when absent."""
    return f"`{value}`" if value else "—"


def render_body(alerts: Sequence[dict[str, Any]], generated_at: datetime) -> str:
    """Render the tracking issue body for the given open alerts."""
    ordered = sort_alerts(alerts)
    numbers = ",".join(str(alert["number"]) for alert in ordered)
    counts = " / ".join(
        f"{severity} {sum(1 for a in ordered if a['security_vulnerability']['severity'] == severity)}"
        for severity in SEVERITIES
    )

    lines = [
        f"{MARKER_PREFIX} alerts={numbers} -->",
        "",
        "Dependabot が検出した未解決の脆弱性アラートです。",
        "このリポジトリでは自動修正 PR (Dependabot security updates) を無効にしているため、",
        "バージョン更新・テスト修正・動作確認をまとめてこの issue で対応します。",
        "",
        f"**未解決 {len(ordered)} 件** — {counts}",
        "",
        "| Severity | Package | 影響範囲 | 修正版 | Advisory | Manifest |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    for alert in ordered:
        vulnerability = alert["security_vulnerability"]
        package = alert["dependency"]["package"]
        patched = (vulnerability.get("first_patched_version") or {}).get("identifier")
        ghsa_id = alert["security_advisory"]["ghsa_id"]
        lines.append(
            "| {severity} | `{ecosystem}` / `{name}` | {vulnerable} | {patched} "
            "| [{ghsa_id}]({url}) | {manifest} |".format(
                severity=vulnerability["severity"],
                ecosystem=package["ecosystem"],
                name=package["name"],
                vulnerable=_cell(vulnerability.get("vulnerable_version_range")),
                patched=_cell(patched),
                ghsa_id=ghsa_id,
                url=alert["html_url"],
                manifest=_cell(alert["dependency"].get("manifest_path")),
            )
        )

    lines += ["", "<details><summary>Advisory の概要</summary>", ""]
    for alert in ordered:
        advisory = alert["security_advisory"]
        lines.append(f"- **{advisory['ghsa_id']}**: {advisory['summary']}")
    lines += ["", "</details>", ""]

    lines += [
        "---",
        f"最終更新: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} ・ "
        f"生成元: `{WORKFLOW_PATH}`",
        "アラートが全て解消されると、この issue は次回実行時に自動でクローズされます。",
    ]
    return "\n".join(lines) + "\n"


def render_new_alert_comment(
    alerts: Sequence[dict[str, Any]], new_numbers: set[int]
) -> str:
    """Render the comment posted when alerts appear that were not reported yet."""
    added = [alert for alert in sort_alerts(alerts) if alert["number"] in new_numbers]
    lines = [f"新しい脆弱性アラートを {len(added)} 件検出しました。"]
    lines.append("")
    for alert in added:
        vulnerability = alert["security_vulnerability"]
        package = alert["dependency"]["package"]
        lines.append(
            f"- **{vulnerability['severity']}** `{package['ecosystem']}` / "
            f"`{package['name']}` — [{alert['security_advisory']['ghsa_id']}]"
            f"({alert['html_url']})"
        )
    lines += ["", "詳細は issue 本文の一覧を参照してください。"]
    return "\n".join(lines) + "\n"


def _gh(args: Sequence[str], stdin: str | None = None) -> str:
    """Run a gh subcommand and return its stdout."""
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def fetch_open_alerts(repo: str) -> list[dict[str, Any]]:
    """Fetch every open Dependabot alert for the repository."""
    raw = _gh(
        [
            "api",
            "--paginate",
            f"/repos/{repo}/dependabot/alerts?state=open&per_page=100",
        ]
    )
    return json.loads(raw)


def find_digest_issue(repo: str) -> dict[str, Any] | None:
    """Return the open tracking issue produced by this script, if any."""
    raw = _gh(
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
        if MARKER_PREFIX in (issue.get("body") or ""):
            return issue
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Reconcile the tracking issue with the current open Dependabot alerts."""
    parser = argparse.ArgumentParser(description=__doc__)
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
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset.")

    alerts = fetch_open_alerts(args.repo)
    issue = find_digest_issue(args.repo)
    print(
        f"open alerts: {len(alerts)}; tracking issue: {issue['number'] if issue else 'none'}"
    )

    if not alerts:
        if issue is None:
            print("nothing to do: no open alerts and no tracking issue.")
            return 0
        print(f"action: close issue #{issue['number']}")
        if not args.dry_run:
            _gh(
                [
                    "issue",
                    "close",
                    str(issue["number"]),
                    "--repo",
                    args.repo,
                    "--reason",
                    "completed",
                    "--comment",
                    "未解決の Dependabot アラートが無くなったため、この issue をクローズします。",
                ]
            )
        return 0

    body = render_body(alerts, datetime.now(UTC))
    if args.dry_run:
        print("--- issue body ---")
        print(body, end="")
        print("--- end ---")

    if issue is None:
        print("action: create issue")
        if not args.dry_run:
            url = _gh(
                [
                    "issue",
                    "create",
                    "--repo",
                    args.repo,
                    "--title",
                    ISSUE_TITLE,
                    "--body-file",
                    "-",
                ],
                stdin=body,
            ).strip()
            print(f"created: {url}")
        return 0

    new_numbers = {alert["number"] for alert in alerts} - extract_alert_numbers(
        issue.get("body") or ""
    )
    print(f"action: update issue #{issue['number']}; new alerts: {len(new_numbers)}")
    if args.dry_run:
        return 0

    _gh(
        [
            "issue",
            "edit",
            str(issue["number"]),
            "--repo",
            args.repo,
            "--body-file",
            "-",
        ],
        stdin=body,
    )
    if new_numbers:
        _gh(
            [
                "issue",
                "comment",
                str(issue["number"]),
                "--repo",
                args.repo,
                "--body-file",
                "-",
            ],
            stdin=render_new_alert_comment(alerts, new_numbers),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
