"""Tests for the Dependabot alert digest rendering and reconciliation logic.

The script lives under scripts/ rather than in the package, so it is loaded the
same way tests/guildbotics/cli/test_cli_reference.py loads its generator.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "dependabot_alert_digest.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "dependabot_alert_digest", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


digest = _load_script()

GENERATED_AT = datetime(2026, 8, 8, 22, 0, 0, tzinfo=UTC)


def make_alert(
    number: int,
    *,
    severity: str = "high",
    name: str = "GitPython",
    ecosystem: str = "pip",
    vulnerable_range: str | None = "<= 3.1.57",
    patched: str | None = "3.1.58",
    manifest_path: str | None = "uv.lock",
    ghsa_id: str = "GHSA-wvpp-8hx9-p66j",
    summary: str = "Unsafe git option guard bypass",
) -> dict[str, Any]:
    """Build an alert shaped like the Dependabot alerts REST response."""
    return {
        "number": number,
        "html_url": f"https://github.com/o/r/security/dependabot/{number}",
        "dependency": {
            "package": {"ecosystem": ecosystem, "name": name},
            "manifest_path": manifest_path,
        },
        "security_advisory": {"ghsa_id": ghsa_id, "summary": summary},
        "security_vulnerability": {
            "severity": severity,
            "vulnerable_version_range": vulnerable_range,
            "first_patched_version": (
                {"identifier": patched} if patched is not None else None
            ),
        },
    }


def test_severity_rank_orders_known_severities_and_places_unknown_last() -> None:
    ranks = [digest.severity_rank(s) for s in ("critical", "high", "medium", "low")]
    assert ranks == sorted(ranks)
    assert digest.severity_rank("moderate") > ranks[-1]


def test_sort_alerts_orders_by_severity_then_package_then_number() -> None:
    alerts = [
        make_alert(3, severity="low", name="pillow"),
        make_alert(2, severity="critical", name="agno"),
        make_alert(5, severity="high", name="Zzz"),
        make_alert(4, severity="high", name="aaa"),
        make_alert(1, severity="high", name="aaa"),
    ]

    assert [a["number"] for a in digest.sort_alerts(alerts)] == [2, 1, 4, 5, 3]


def test_extract_alert_numbers_round_trips_through_the_rendered_body() -> None:
    alerts = [make_alert(7), make_alert(11, severity="critical")]

    body = digest.render_body(alerts, GENERATED_AT)

    assert digest.extract_alert_numbers(body) == {7, 11}


def test_extract_alert_numbers_returns_empty_for_bodies_without_a_marker() -> None:
    assert digest.extract_alert_numbers("hand written issue body") == set()
    assert digest.extract_alert_numbers(f"{digest.MARKER_PREFIX} alerts= -->") == set()


def test_render_body_reports_counts_and_one_row_per_alert() -> None:
    alerts = [
        make_alert(1, severity="critical", name="agno"),
        make_alert(2, severity="high", name="GitPython"),
        make_alert(3, severity="high", name="pillow"),
    ]

    body = digest.render_body(alerts, GENERATED_AT)

    assert "**未解決 3 件** — critical 1 / high 2 / medium 0 / low 0" in body
    header, separator, *alert_rows = [
        line for line in body.splitlines() if line.startswith("| ")
    ]
    assert header.startswith("| Severity |")
    assert separator.startswith("| --- |")
    assert len(alert_rows) == len(alerts)
    assert "| critical | `pip` / `agno` | `<= 3.1.57` | `3.1.58` |" in body
    assert "2026-08-08 22:00:00 UTC" in body
    assert digest.WORKFLOW_PATH in body


def test_render_body_marks_missing_range_and_patched_version() -> None:
    alerts = [make_alert(1, vulnerable_range=None, patched=None, manifest_path=None)]

    body = digest.render_body(alerts, GENERATED_AT)

    assert "| high | `pip` / `GitPython` | — | — |" in body
    assert body.rstrip().endswith(
        "アラートが全て解消されると、この issue は次回実行時に自動でクローズされます。"
    )


def test_render_body_lists_every_advisory_summary() -> None:
    alerts = [
        make_alert(1, ghsa_id="GHSA-aaaa", summary="first advisory"),
        make_alert(2, ghsa_id="GHSA-bbbb", summary="second advisory"),
    ]

    body = digest.render_body(alerts, GENERATED_AT)

    assert "- **GHSA-aaaa**: first advisory" in body
    assert "- **GHSA-bbbb**: second advisory" in body


def test_render_new_alert_comment_lists_only_the_newly_seen_alerts() -> None:
    alerts = [
        make_alert(1, name="agno"),
        make_alert(2, severity="critical", name="pillow"),
        make_alert(3, name="weasyprint"),
    ]

    comment = digest.render_new_alert_comment(alerts, {2, 3})

    assert comment.startswith("新しい脆弱性アラートを 2 件検出しました。")
    assert "`pillow`" in comment
    assert "`weasyprint`" in comment
    assert "`agno`" not in comment
    assert comment.index("`pillow`") < comment.index("`weasyprint`")
