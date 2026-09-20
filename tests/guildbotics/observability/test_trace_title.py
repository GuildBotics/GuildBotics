"""Unit tests for ``guildbotics.observability.trace_title``."""

from __future__ import annotations

from typing import Any

from guildbotics.observability.trace_title import (
    github_reference_label,
    is_read_only_record,
    resolve_trace_title,
)
from guildbotics.utils.i18n_tool import set_language, t

ISSUE_TITLE = "診断ログの実行タイトルに作業対象の PR / Issue タイトルを表示する"


def _title(
    records: list[dict[str, Any]] | None = None,
    attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> str:
    return resolve_trace_title(
        records or [], attributes or {}, fallback="trace-1", **kwargs
    )


def test_target_title_wins_over_everything_else() -> None:
    title = _title(
        [{"payload": {"title": "PR #544 の作業記録"}}],
        {"github.title": ISSUE_TITLE, "github.kind": "issue", "github.number": "544"},
        command="workflows/ticket_driven_workflow",
        completion_summary="Issue #544 を修正\n詳細",
    )
    assert title == ISSUE_TITLE


def test_completion_summary_first_line_titles_a_trace_without_a_target() -> None:
    assert _title(completion_summary="請求プランの質問に回答\n詳細は省略") == (
        "請求プランの質問に回答"
    )


def test_target_reference_is_used_when_the_title_is_unknown() -> None:
    assert _title(attributes={"github.kind": "pull_request", "github.number": "7"}) == (
        "PR #7"
    )


def test_read_only_records_do_not_title_the_trace() -> None:
    records = [
        {
            "attributes": {"memory.action": "recall"},
            "payload": {"title": "Memory recall"},
        },
        {
            "attributes": {"memory.action": "update"},
            "payload": {"title": "PR #247: レビュー対応"},
        },
    ]
    assert _title(records) == "PR #247: レビュー対応"


def test_chat_trigger_label_precedes_the_prompt() -> None:
    set_language("ja")
    title = _title(
        [{"payload": {"fields": {"prompt": "長い system prompt"}}}],
        {"event.provider": "slack"},
    )
    assert title == t("observability.trace_title.chat_trigger", provider="Slack")


def test_falls_back_through_command_to_the_trace_id() -> None:
    assert _title(command="member memory recall") == "member memory recall"
    assert _title() == "trace-1"


def test_inspected_github_target_is_read_only() -> None:
    assert is_read_only_record({"attributes": {"github.action": "inspected"}})
    assert not is_read_only_record({"attributes": {"github.action": ""}})
    assert is_read_only_record({"attributes": {"memory.action": "touch"}})


def test_github_reference_label_names_pull_requests_and_issues() -> None:
    assert github_reference_label("pull_request", "7") == "PR #7"
    assert github_reference_label("issue", "42") == "Issue #42"
    assert github_reference_label("", "9") == "GitHub #9"
    assert github_reference_label("issue", "") == ""
