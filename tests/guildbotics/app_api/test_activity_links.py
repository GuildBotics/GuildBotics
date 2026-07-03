import pytest

from guildbotics.app_api.activity_links import (
    doc_link_from_memory,
    links_from_attributes,
)


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
