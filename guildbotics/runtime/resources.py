"""What the member's read commands return: pages of Branvil's own views of a
ticket's, a pull request's, or a repository's resources.

Tickets and pull requests are named by their URLs, and the comments, reviews,
threads, and jobs in them by opaque ids that mean something only together
with the item's URL. A repository is named by its path. The host checks every
read's conditions before it reads.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.process_limits import STREAM_READ_LIMIT

# Room for JSON escaping in CLI, member result, and host envelope.
MAX_PAGE_BYTES = STREAM_READ_LIMIT // 16
MAX_LOG_TAIL_BYTES = MAX_PAGE_BYTES // 10


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadinessQuery(ReadModel):
    failed_logs: bool = False
    log_tail_bytes: int = Field(default=MAX_LOG_TAIL_BYTES, ge=1, le=MAX_LOG_TAIL_BYTES)


#: What a resource is part of, which its subject names: a ticket or a pull
#: request by its URL, a repository by its path.
ReadSubject = Literal["ticket", "pull_request", "repository"]

#: Every resource, by what it is part of. ``tracker_tickets`` takes the
#: repository path the tickets are kept in rather than a ticket's URL.
RESOURCE_SUBJECTS: dict[str, ReadSubject] = {
    "tracker_tickets": "ticket",
    "tickets": "ticket",
    "ticket_comments": "ticket",
    "ticket_attachments": "ticket",
    "ticket_pull_requests": "ticket",
    "pull_requests": "pull_request",
    "pull_request_comments": "pull_request",
    "pull_request_reviews": "pull_request",
    "pull_request_files": "pull_request",
    "pull_request_threads": "pull_request",
    "pull_request_readiness": "pull_request",
    "dependency_alerts": "repository",
}
#: The resources the ticket service reads; the code host reads the others,
#: the pull requests of a ticket among them.
TICKET_RESOURCES = frozenset(
    {"tracker_tickets", "tickets", "ticket_comments", "ticket_attachments"}
)


class ReadPage(ReadModel):
    """One bounded page of a resource's items."""

    items: list[dict[str, Any]]
    continuation: str | None = None
