"""Combine a ticket's resources into a complete inspection, without inference."""

from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.commands.repository import RepositoryReader, display

COMMAND_METADATA = {
    "description": {
        "en": "Inspect a ticket, its comments, its lane, its related tickets, its attachments, and the pull requests that refer to it.",
        "ja": "チケットの本文・コメント・レーン・関係するチケット・添付と、参照している PR を確認します。",
    },
    "read_only": True,
}


async def main(context: Any, url: str) -> str:
    reader = RepositoryReader(context, url)
    result = await reader.one("tickets")
    result["comments"] = await reader.read("ticket_comments")
    try:
        result["attachments"] = await reader.read("ticket_attachments")
    except CommandError as exc:
        # The attachments add to what the ticket says: a service that cannot
        # list them leaves the rest of the ticket to read, with the reason.
        result["attachments"] = None
        result["attachments_unavailable"] = str(exc)
    result["linked_pull_request_candidates"] = list(
        {
            pull["url"]: pull for pull in await reader.read("ticket_pull_requests")
        }.values()
    )
    return display(result)
