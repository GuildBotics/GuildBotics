import pytest

from guildbotics.app_api.activity_links import (
    dedupe_links,
    doc_link_from_memory,
    links_from_attributes,
    links_from_record,
    memory_diagnostics_url,
)
from guildbotics.app_api.models import ActivityHistoryLink


def test_github_link_kind_comes_from_provider_attributes_not_url_path() -> None:
    links = links_from_attributes(
        {
            "github.kind": "issue",
            "github.number": "8",
            "github.url": "https://github.com/owner/repo/pull/8",
        }
    )

    assert [(link.kind, link.label, link.url) for link in links] == [
        ("issue", "Issue #8", "https://github.com/owner/repo/pull/8")
    ]


def test_unknown_github_link_kind_is_external() -> None:
    links = links_from_attributes(
        {
            "github.number": "8",
            "github.url": "https://github.com/owner/repo/pull/8",
        }
    )

    assert [(link.kind, link.label, link.url) for link in links] == [
        ("external", "GitHub #8", "https://github.com/owner/repo/pull/8")
    ]


@pytest.mark.parametrize("action", ["recall", "get", "touch"])
def test_read_only_memory_actions_produce_no_doc_link(action: str) -> None:
    link = doc_link_from_memory(
        {"title": "Issue #170 作業記録"},
        {"memory.action": action, "memory.doc_id": "doc-1"},
    )

    assert link is None


@pytest.mark.parametrize("action", ["record", "update", "archive", "promote"])
def test_content_changing_memory_actions_produce_doc_link(action: str) -> None:
    link = doc_link_from_memory(
        {"title": "Issue #170 作業記録"},
        {"memory.action": action, "memory.doc_id": "doc-1"},
    )

    assert link is not None
    assert link.kind == "doc"
    assert link.label == "Issue #170 作業記録"


def test_read_only_memory_record_yields_no_links() -> None:
    # A `get`/`touch`/`recall` references documents (and their source PRs), but
    # those are not the session's work, so no links (not even the source PR).
    links = links_from_record(
        {
            "title": "PR #203 Chat workflow backfill",
            "source": [{"type": "pr", "url": "https://github.com/o/r/pull/203"}],
        },
        {"memory.action": "get", "memory.doc_id": "d1"},
    )

    assert links == []


def test_content_changing_memory_record_yields_links() -> None:
    links = links_from_record(
        {
            "title": "PR #246 の作業記録",
            "source": [{"type": "pr", "url": "https://github.com/o/r/pull/246"}],
        },
        {"memory.action": "record", "memory.doc_id": "d1"},
    )

    kinds = {link.kind for link in links}
    assert "pull_request" in kinds
    assert "doc" in kinds


def test_memory_diagnostics_url_namespaces_the_trace_filter() -> None:
    url = memory_diagnostics_url(
        {"memory.action": "record", "memory.doc_id": "d1"},
        {
            "trace_id": "trace-1",
            "timestamp": "2026-07-01T10:00:00Z",
            "person_id": "alice",
        },
    )

    assert "memory_trace_id=trace-1" in url
    assert "&trace_id=" not in url


def test_dedupe_collapses_same_pr_url_with_different_labels() -> None:
    url = "https://github.com/o/r/pull/246"
    deduped = dedupe_links(
        [
            ActivityHistoryLink(kind="pull_request", label="PR #246", url=url),
            ActivityHistoryLink(
                kind="pull_request", label="PR #246 長い note 名", url=url
            ),
        ]
    )

    assert [(link.kind, link.label) for link in deduped] == [
        ("pull_request", "PR #246")
    ]
