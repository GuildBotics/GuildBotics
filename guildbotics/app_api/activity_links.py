from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, cast
from urllib.parse import urlparse

from guildbotics.app_api.activity_events import commit_entries, commit_message
from guildbotics.app_api.models import ActivityHistoryLink

type ActivityLinkKind = Literal["doc", "issue", "pull_request", "commit", "external"]


def links_from_records(
    records: list[dict[str, Any]], attributes: dict[str, Any]
) -> list[ActivityHistoryLink]:
    links = links_from_attributes(attributes)
    for item in records:
        payload = (
            cast(dict[str, Any], item.get("payload"))
            if isinstance(item.get("payload"), dict)
            else {}
        )
        item_attributes = (
            cast(dict[str, Any], item.get("attributes"))
            if isinstance(item.get("attributes"), dict)
            else {}
        )
        links.extend(links_from_record(payload, item_attributes))
    return dedupe_links(links)


def links_from_record(
    payload: dict[str, Any], attributes: dict[str, Any]
) -> list[ActivityHistoryLink]:
    links = links_from_attributes(attributes)
    source_links = source_links_from_payload(payload)
    if source_links:
        links.extend(source_links)
    elif (doc_link := doc_link_from_memory(payload, attributes)) is not None:
        links.append(doc_link)
    links.extend(commit_links(payload))
    return dedupe_links(links)


def links_from_attributes(attributes: dict[str, Any]) -> list[ActivityHistoryLink]:
    url = str(attributes.get("github.url") or "")
    number = str(attributes.get("github.number") or "")
    kind = str(attributes.get("github.kind") or "")
    if not (url or number):
        return []
    link_kind = github_link_kind(kind)
    label = github_link_label(link_kind, number, url)
    return [ActivityHistoryLink(kind=link_kind, label=label, url=url)]


def github_link_kind(kind: str) -> ActivityLinkKind:
    if kind == "pull_request":
        return "pull_request"
    if kind == "issue":
        return "issue"
    return "external"


def github_link_label(kind: ActivityLinkKind, number: str, url: str) -> str:
    if kind == "pull_request":
        return f"PR #{number}" if number else url
    if kind == "issue":
        return f"Issue #{number}" if number else url
    return f"GitHub #{number}" if number else url


def doc_link_from_memory(
    payload: dict[str, Any], attributes: dict[str, Any]
) -> ActivityHistoryLink | None:
    path = str(attributes.get("memory.path") or "")
    doc_id = str(attributes.get("memory.doc_id") or "")
    title = str(payload.get("title") or "")
    label = title or doc_id or path
    if not label:
        return None
    return ActivityHistoryLink(kind="doc", label=label, url="")


def source_links_from_payload(payload: dict[str, Any]) -> list[ActivityHistoryLink]:
    source = payload.get("source")
    if not isinstance(source, list):
        return []
    entries = [item for item in source if isinstance(item, dict)]
    use_title = len(entries) == 1
    links: list[ActivityHistoryLink] = []
    for item in entries:
        link = source_link(item, str(payload.get("title") or "") if use_title else "")
        if link is not None:
            links.append(link)
    return links


def source_link(item: dict[str, Any], title: str) -> ActivityHistoryLink | None:
    url = str(item.get("url") or "")
    if not url:
        return None
    source_type = str(item.get("type") or "").lower()
    kind = source_link_kind(source_type)
    label = title or source_link_label(kind, url, source_type)
    return ActivityHistoryLink(kind=kind, label=label, url=url)


def source_link_kind(source_type: str) -> ActivityLinkKind:
    if source_type in {"pr", "pull_request"}:
        return "pull_request"
    if source_type in {"ticket", "issue"}:
        return "issue"
    return "external"


def source_link_label(kind: ActivityLinkKind, url: str, source_type: str) -> str:
    number = last_url_segment(url)
    if kind == "pull_request":
        return f"PR #{number}" if number else "Pull request"
    if kind == "issue":
        return f"Issue #{number}" if number else "Issue"
    return source_type.title() if source_type else url


def last_url_segment(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    value = path.rsplit("/", 1)[-1] if path else ""
    return value if value.isdigit() else ""


def commit_links(payload: dict[str, Any]) -> list[ActivityHistoryLink]:
    links: list[ActivityHistoryLink] = []
    for commit in commit_entries(payload):
        url = str(commit.get("url") or commit.get("html_url") or "")
        sha = str(commit.get("id") or commit.get("sha") or "")
        if not (url or sha):
            continue
        links.append(
            ActivityHistoryLink(
                kind="commit",
                label=commit_message(commit) or (sha[:7] if sha else url),
                url=url,
            )
        )
    return links


def dedupe_links(
    links: Iterable[ActivityHistoryLink],
) -> list[ActivityHistoryLink]:
    deduped: list[ActivityHistoryLink] = []
    seen: set[tuple[str, str, str]] = set()
    for link in links:
        key = (link.kind, link.label, link.url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(link)
    return deduped
