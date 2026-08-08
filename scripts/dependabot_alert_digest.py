#!/usr/bin/env python3
"""Mirror open Dependabot alerts into a single tracking issue.

Dependabot security updates (the automatic fix PRs) are deliberately disabled
for this repository: a bare version bump without the accompanying test and
behaviour fixes is not a reviewable change. This script instead reflects the
open alerts into one tracking issue, so the work enters the normal ticket flow.

Issue lifecycle and marker handling live in tracking_issue.py, which this script
shares with the outdated dependency digest.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

import tracking_issue

MARKER_PREFIX = "<!-- dependabot-alert-digest"
ISSUE_TITLE = "Dependabot の未解決脆弱性アラート"
WORKFLOW_PATH = ".github/workflows/dependabot-alert-digest.yml"
CLOSE_COMMENT = (
    "未解決の Dependabot アラートが無くなったため、この issue をクローズします。"
)

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


def _cell(value: str | None) -> str:
    """Render a table cell as inline code, or as an em dash when absent."""
    return f"`{value}`" if value else "—"


def render_body(alerts: Sequence[dict[str, Any]], generated_at: datetime) -> str:
    """Render the tracking issue body for the given open alerts."""
    ordered = sort_alerts(alerts)
    counts = " / ".join(
        f"{severity} "
        f"{sum(1 for a in ordered if a['security_vulnerability']['severity'] == severity)}"
        for severity in SEVERITIES
    )

    lines = [
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
        lines.append(
            "| {severity} | `{ecosystem}` / `{name}` | {vulnerable} | {patched} "
            "| [{ghsa_id}]({url}) | {manifest} |".format(
                severity=vulnerability["severity"],
                ecosystem=package["ecosystem"],
                name=package["name"],
                vulnerable=_cell(vulnerability.get("vulnerable_version_range")),
                patched=_cell(patched),
                ghsa_id=alert["security_advisory"]["ghsa_id"],
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
    alerts: Sequence[dict[str, Any]], new_ids: set[str]
) -> str:
    """Render the comment posted when alerts appear that were not reported yet."""
    added = [a for a in sort_alerts(alerts) if str(a["number"]) in new_ids]
    lines = [f"新しい脆弱性アラートを {len(added)} 件検出しました。", ""]
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


def fetch_open_alerts(repo: str) -> list[dict[str, Any]]:
    """Fetch every open Dependabot alert for the repository."""
    raw = tracking_issue.gh(
        [
            "api",
            "--paginate",
            f"/repos/{repo}/dependabot/alerts?state=open&per_page=100",
        ]
    )
    return json.loads(raw)


def main(argv: Sequence[str] | None = None) -> int:
    """Reconcile the tracking issue with the current open Dependabot alerts."""
    parser = tracking_issue.common_parser(__doc__ or "")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset.")

    alerts = fetch_open_alerts(args.repo)
    ordered = sort_alerts(alerts)
    return tracking_issue.reconcile(
        args.repo,
        prefix=MARKER_PREFIX,
        title=ISSUE_TITLE,
        body=render_body(ordered, datetime.now(UTC)),
        item_ids=[str(alert["number"]) for alert in ordered],
        close_comment=CLOSE_COMMENT,
        on_new_items=lambda new_ids: render_new_alert_comment(ordered, new_ids),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
