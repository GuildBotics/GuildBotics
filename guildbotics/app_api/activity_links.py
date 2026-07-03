from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, cast
from urllib.parse import urlencode, urlparse

from guildbotics.app_api.activity_events import commit_entries, commit_message
from guildbotics.app_api.models import ActivityHistoryLink

type ActivityLinkKind = Literal["doc", "issue", "pull_request", "commit", "external"]
# Read-only / signal memory actions do not change a document, so they are noise
# in activity history (they still appear in the dedicated Memory audit view).
# Only content-changing actions (record, update, archive, promote) get a link.
MEMORY_READ_ONLY_ACTIONS = frozenset({"recall", "get", "touch"})


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
        links.extend(links_from_record(payload, item_attributes, item))
    return dedupe_links(links)


def links_from_record(
    payload: dict[str, Any],
    attributes: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> list[ActivityHistoryLink]:
    timestamp = record_timestamp(record)
    links = links_from_attributes(attributes, timestamp=timestamp)
    source_links = source_links_from_payload(payload, timestamp=timestamp)
    if source_links:
        links.extend(source_links)
    if (doc_link := doc_link_from_memory(payload, attributes, record)) is not None:
        links.append(doc_link)
    links.extend(commit_links(payload, timestamp=timestamp))
    return dedupe_links(links)


def links_from_attributes(
    attributes: dict[str, Any],
    *,
    timestamp: str = "",
) -> list[ActivityHistoryLink]:
    url = str(attributes.get("github.url") or "")
    number = str(attributes.get("github.number") or "")
    kind = str(attributes.get("github.kind") or "")
    if not (url or number):
        return []
    link_kind = github_link_kind(kind)
    label = github_link_label(link_kind, number, url)
    return [
        ActivityHistoryLink(kind=link_kind, label=label, url=url, timestamp=timestamp)
    ]


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
    payload: dict[str, Any],
    attributes: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> ActivityHistoryLink | None:
    if str(attributes.get("memory.action") or "") in MEMORY_READ_ONLY_ACTIONS:
        return None
    path = str(attributes.get("memory.path") or "")
    doc_id = str(attributes.get("memory.doc_id") or "")
    title = str(payload.get("title") or "")
    label = title or doc_id or path
    if not label:
        return None
    return ActivityHistoryLink(
        kind="doc",
        label=label,
        url=memory_diagnostics_url(attributes, record),
        timestamp=record_timestamp(record),
    )


def memory_diagnostics_url(
    attributes: dict[str, Any],
    record: dict[str, Any] | None,
) -> str:
    params = {
        "tab": "memory",
        "doc_id": str(attributes.get("memory.doc_id") or ""),
        "trace_id": str(record.get("trace_id") or "") if record else "",
        "timestamp": str(record.get("timestamp") or "") if record else "",
        "action": str(attributes.get("memory.action") or ""),
        "person_id": str(record.get("person_id") or "") if record else "",
    }
    query = urlencode({key: value for key, value in params.items() if value})
    return f"/diagnostics?{query}" if query else "/diagnostics?tab=memory"


def source_links_from_payload(
    payload: dict[str, Any],
    *,
    timestamp: str = "",
) -> list[ActivityHistoryLink]:
    source = payload.get("source")
    if not isinstance(source, list):
        return []
    entries = [item for item in source if isinstance(item, dict)]
    use_title = len(entries) == 1
    links: list[ActivityHistoryLink] = []
    for item in entries:
        link = source_link(
            item,
            str(payload.get("title") or "") if use_title else "",
            timestamp=timestamp,
        )
        if link is not None:
            links.append(link)
    return links


def source_link(
    item: dict[str, Any],
    title: str,
    *,
    timestamp: str = "",
) -> ActivityHistoryLink | None:
    url = str(item.get("url") or "")
    if not url:
        return None
    source_type = str(item.get("type") or "").lower()
    kind = source_link_kind(source_type)
    label = title or source_link_label(kind, url, source_type)
    return ActivityHistoryLink(kind=kind, label=label, url=url, timestamp=timestamp)


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


def commit_links(
    payload: dict[str, Any], *, timestamp: str = ""
) -> list[ActivityHistoryLink]:
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
                timestamp=timestamp,
            )
        )
    return links


def record_timestamp(record: dict[str, Any] | None) -> str:
    return str(record.get("timestamp") or "") if record else ""


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
