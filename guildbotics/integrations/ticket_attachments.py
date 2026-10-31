"""Which attachment a ticket hands out, whichever service keeps it.

A provider lists the attachments a ticket refers to; an id is the ticket's
only while it is among them, and its content is handed out only within the
limit the caller asks for.
"""

from __future__ import annotations

from collections.abc import Iterable

from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.runtime.resources import Attachment
from guildbotics.utils.i18n_tool import t


def referred(
    attachments: Iterable[Attachment], url: str, attachment_id: str, limit: int
) -> Attachment:
    """The attachment ``attachment_id`` among ``attachments``, those the
    ticket ``url`` refers to, if it is at most ``limit`` bytes.

    Raises:
        ServiceNotFound: If the ticket refers to no such attachment.
        ServiceError: :func:`too_large`, if it is above ``limit``.
    """
    found = next(
        (attachment for attachment in attachments if attachment.id == attachment_id),
        None,
    )
    if found is None:
        raise ServiceNotFound(
            t("integrations.resources.attachment_missing", url=url, id=attachment_id)
        )
    if found.size > limit:
        raise too_large(found, limit)
    return found


def too_large(attachment: Attachment, limit: int) -> ServiceError:
    """What refuses the content of ``attachment`` above ``limit`` bytes."""
    return ServiceError(
        t(
            "integrations.resources.attachment_too_large",
            name=attachment.name,
            limit=limit,
        )
    )
