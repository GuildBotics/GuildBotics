"""What the local code host and tickets keep of an item alike: its work
target, the comments, reviews, and inline comments in it, and their pages."""

from __future__ import annotations

from typing import Any

from guildbotics.entities import Task
from guildbotics.integrations.local import store
from guildbotics.runtime.port import (
    SEMANTIC_REACTIONS,
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    CommentWritten,
    ReactionAdded,
    ReadPage,
    WorkTarget,
)
from guildbotics.utils.i18n_tool import t


def target(ref: store.Ref, item: dict[str, Any]) -> WorkTarget:
    return WorkTarget(kind=ref.kind, url=ref.url, title=item["title"], label=ref.label)


def new_item(number: int, title: str, body: str, author: str) -> dict[str, Any]:
    """What a new pull request or ticket starts with."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "open",
        "author": author,
        "labels": [],
        "assignees": [],
        "created_at": store.now(),
        "closed_at": None,
        "comments": [],
    }


def task(ref: store.Ref, item: dict[str, Any], **fields: Any) -> Task:
    """The work ``item`` is, for the member it asks something of."""
    return Task(
        id=ref.url,
        url=ref.url,
        title=item["title"],
        description=item["body"],
        created_at=item["created_at"],
        **fields,
    )


def comment(
    scope: str,
    ref: store.Ref,
    author: str,
    body: str,
    status: WorkflowStatus | None,
) -> CommentWritten:
    """Add a comment to the item ``ref`` names, writing in ``scope``."""
    item = store.load(ref)
    written = entry(item, "comment", author, body, status=status)
    item["comments"].append(written)
    store.save(scope, ref, item)
    return CommentWritten(
        id=written["id"],
        url=None,
        author=author,
        created_at=written["created_at"],
        target=target(ref, item),
    )


def react(
    scope: str,
    ref: store.Ref,
    author: str,
    target_id: str,
    reaction: SemanticReaction,
) -> ReactionAdded:
    """React to what the item ``ref`` names reads as ``target_id``.

    Raises:
        ServiceUnsupported: If ``reaction`` is no meaning a reaction has.
    """
    if reaction not in SEMANTIC_REACTIONS:
        raise ServiceUnsupported(f"Unsupported semantic reaction: {reaction}")
    item = store.load(ref)
    find_entry(ref, item, target_id).setdefault("reactions", []).append(
        {"author": author, "reaction": reaction}
    )
    store.save(scope, ref, item)
    return ReactionAdded(reaction=reaction, id=target_id)


def entry(
    item: dict[str, Any],
    kind: str,
    author: str,
    body: str,
    *,
    status: WorkflowStatus | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A new entry of ``kind`` (``comment``, ``review``, ``inline``) of the item."""
    written = {
        "id": store.next_id(item, kind),
        "author": author,
        "body": body.rstrip(),
        "created_at": store.now(),
        **fields,
    }
    if status is not None:
        written["status"] = status.payload()
    return written


def find_entry(ref: store.Ref, item: dict[str, Any], entry_id: str) -> dict[str, Any]:
    """The entry of the item its reads named ``entry_id``.

    Raises:
        ServiceNotFound: If it has none.
    """
    found = next(
        (e for key in store.ENTRIES for e in item.get(key, []) if e["id"] == entry_id),
        None,
    )
    if found is None:
        raise ServiceNotFound(f"'{entry_id}' is none of {ref.url}.")
    return found


def page(
    resource: str,
    entries: list[dict[str, Any]],
    conditions: dict[str, Any],
    continuation: str,
) -> ReadPage:
    """The page of ``entries`` the continuation (an offset) asks for.

    Raises:
        ServiceError: If it is not one a page gave.
    """
    if continuation and not continuation.isdigit():
        raise ServiceError(t("integrations.resources.continuation"))
    start = int(continuation or 0)
    end = start + conditions["page_size"]
    return ReadPage.of(
        resource,
        entries[start:end],
        continuation=str(end) if end < len(entries) else None,
    )
