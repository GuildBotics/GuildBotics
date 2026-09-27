"""The member's services, as a command inside its isolated environment
reaches them.

The member's credentials never leave the host, so the services a command's
code uses (:class:`ChatService`, :class:`TicketManager`) are the member's own
commands, run by the host through the command's window as an agent's are:
the command can do with them only what may be handed to an agent.
"""

from __future__ import annotations

import json
from logging import Logger
from typing import Any

from guildbotics.entities import Person, Team
from guildbotics.integrations.chat_service import (
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
    ChatService,
)
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.agent_runtime.host_client import HostClient
from guildbotics.runtime.integration_factory import IntegrationFactory


class MemberCommandError(RuntimeError):
    """A member command the window ran failed; the message is its error."""


class WindowChatService(ChatService):
    """The member's chat, through the member's chat commands."""

    def __init__(self, client: HostClient, person_id: str) -> None:
        self._client = client
        self._person_id = person_id

    async def get_bot_identity(self) -> ChatIdentity:
        identity = await self._member("identity")
        return ChatIdentity(identity["user_id"], identity["display_name"])

    async def list_channel_events(self, channel_id: str, **_: Any) -> ChatEventPage:
        raise _unavailable("Reading a channel's events")

    async def list_thread_events(self, channel_id: str, **_: Any) -> ChatEventPage:
        raise _unavailable("Reading a thread's events")

    async def resolve_channel_id(self, channel_name: str) -> str | None:
        found = await self._member("resolve-channel", "--channel-name", channel_name)
        return found["channel_id"] or None

    async def post_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        if metadata:
            raise _unavailable("Posting with message metadata")
        where = ("reply", "--thread-ts", thread_ts) if thread_ts else ("post",)
        posted = await self._member(
            *where, "--channel-id", channel_id, "--content-stdin", stdin=text
        )
        return ChatPostResult(
            posted["channel_id"], posted["message_ts"], posted["thread_ts"]
        )

    async def add_reaction(
        self, channel_id: str, message_ts: str, reaction: str
    ) -> None:
        await self._member(
            "reaction",
            "add",
            "--channel-id",
            channel_id,
            "--message-ts",
            message_ts,
            "--reaction",
            reaction,
        )

    async def _member(self, *arguments: str, stdin: str = "") -> dict[str, Any]:
        """Run the member's chat command ``arguments`` and read its result."""
        result = await self._client.acall(
            "member",
            arguments=[
                "chat",
                *arguments,
                "--person",
                self._person_id,
                "--service",
                "slack",
                "--format",
                "json",
            ],
            stdin=stdin,
        )
        if result["exit_code"]:
            raise MemberCommandError(
                result["stderr"].strip()
                or f"The member command exited with {result['exit_code']}."
            )
        return json.loads(result["stdout"])


class WindowIntegrationFactory(IntegrationFactory):
    """The member's services for a command inside its isolated environment,
    through the command's window ``client``."""

    def __init__(self, client: HostClient) -> None:
        self._client = client

    def create_ticket_manager(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketManager:
        # Each of its operations is the host's ticket selection or has no
        # member command that does the same.
        raise _unavailable("The ticket manager")

    def create_chat_service(
        self, logger: Logger, person: Person, team: Team
    ) -> ChatService:
        return WindowChatService(self._client, person.person_id)


def _unavailable(what: str) -> NotImplementedError:
    return NotImplementedError(
        f"{what} is not available inside a command's isolated environment."
    )
