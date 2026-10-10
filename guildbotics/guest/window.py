"""The member's services and the external inference calls, as a command
inside its isolated environment reaches them.

The member's credentials never leave the host, so the services a command's
code uses (:class:`ChatService`, :class:`TicketManager`) are the member's own
commands, run by the host through the command's window as an agent's are:
the command can do with them only what may be handed to an agent. The
external inference calls (:class:`WindowInference`) go to the host the same
way, where the keys are.
"""

from __future__ import annotations

import json
from datetime import datetime
from logging import Logger
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.entities import Person, Team
from guildbotics.guest.host_client import HostClient
from guildbotics.intelligences.agent_runtime.wire import HostCallError
from guildbotics.intelligences.brains.inference import AgnoAnswer, AgnoCall, JevCall
from guildbotics.runtime.chat_service import (
    ChatEventPage,
    ChatIdentity,
    ChatMessageRef,
    ChatPostResult,
    ChatService,
    ChatServiceError,
    CredentialCheck,
)
from guildbotics.runtime.code_hosting_resources import (
    RepositoryReadError,
    RepositoryReadPage,
)
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.integration_factory import IntegrationFactory
from guildbotics.runtime.ticket_manager import TicketManager


class MemberCommandError(RuntimeError):
    """A member command the window ran failed; the message is its error."""


class WindowCodeHostingService(CodeHostingService):
    """The same repository contract, via the command's member grant."""

    def __init__(self, client: HostClient, person_id: str) -> None:
        self._client = client
        self._person_id = person_id

    async def read(
        self,
        resource: str,
        repo: str,
        *,
        identifier: str = "",
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> RepositoryReadPage:
        result = await _member_result(
            self._client,
            [
                "repository",
                "read",
                "--person",
                self._person_id,
                "--resource",
                resource,
                "--repo",
                repo,
                "--identifier",
                identifier,
                "--params",
                json.dumps(parameters if parameters is not None else {}),
                "--continuation",
                continuation,
                "--format",
                "json",
            ],
            failure=RepositoryReadError,
        )
        page = RepositoryReadPage.model_validate(result)
        # What the host answered is held to the resource's schema here too: a
        # command reads each item in its full shape, defaults included.
        return RepositoryReadPage.of(
            resource, page.items, continuation=page.continuation, target=page.target
        )

    async def aclose(self) -> None:
        """The command owns the shared host client."""


#: How a member command reports a failure it anticipated (``ClickException``).
_REPORTED_FAILURE = "Error: "


async def _member_result(
    client: HostClient,
    arguments: list[str],
    stdin: str = "",
    *,
    failure: type[Exception] = MemberCommandError,
) -> dict[str, Any]:
    """Run a member command and read its JSON result.

    Raises:
        Exception: ``failure`` with the command's own message when it reported
            the failure; ``MemberCommandError`` for anything else, such as an
            uncaught error, whose text is not meant for the user.
    """
    result = await client.acall("member", arguments=arguments, stdin=stdin)
    if result["exit_code"]:
        stderr = result["stderr"].strip()
        if stderr.startswith(_REPORTED_FAILURE):
            raise failure(stderr.removeprefix(_REPORTED_FAILURE))
        raise MemberCommandError(
            stderr or f"The member command exited with {result['exit_code']}."
        )
    return json.loads(result["stdout"])


class WindowChatService(ChatService):
    """The member's chat, through the member's chat commands."""

    def __init__(self, client: HostClient, person_id: str) -> None:
        self._client = client
        self._person_id = person_id

    async def get_bot_identity(self) -> ChatIdentity:
        identity = await self._member("identity")
        return ChatIdentity(identity["user_id"], identity["display_name"])

    async def check_credentials(self) -> list[CredentialCheck]:
        raise _unavailable("Checking chat credentials")

    def self_user_id(self, person: Person) -> str:
        raise _unavailable("Reading a member's chat user")

    def parse_message_url(self, url: str) -> ChatMessageRef:
        raise _unavailable("Reading a chat message URL")

    def mentioned_user_ids(self, text: str) -> list[str]:
        raise _unavailable("Reading chat mentions")

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
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ChatPostResult:
        if metadata:
            raise _unavailable("Posting with message metadata")
        where = ("reply", "--thread-id", thread_id) if thread_id else ("post",)
        posted = await self._member(
            *where, "--channel-id", channel_id, "--content-stdin", stdin=text
        )
        return ChatPostResult(
            posted["channel_id"],
            posted["message_id"],
            posted["thread_id"],
            datetime.fromisoformat(posted["occurred_at"]),
        )

    async def add_reaction(
        self, channel_id: str, message_id: str, reaction: str
    ) -> None:
        await self._member(
            "reaction",
            "add",
            "--channel-id",
            channel_id,
            "--message-id",
            message_id,
            "--reaction",
            reaction,
        )

    async def _member(self, *arguments: str, stdin: str = "") -> dict[str, Any]:
        """Run the member's chat command ``arguments`` and read its result."""
        return await _member_result(
            self._client,
            [
                "chat",
                *arguments,
                "--person",
                self._person_id,
                "--format",
                "json",
            ],
            stdin=stdin,
            failure=ChatServiceError,
        )


class WindowIntegrationFactory(IntegrationFactory):
    """The member's services for a command inside its isolated environment,
    through the command's window ``client``."""

    def __init__(self, client: HostClient) -> None:
        self._client = client

    def create_code_hosting_service(
        self, logger: Logger, person: Person, team: Team
    ) -> CodeHostingService:
        return WindowCodeHostingService(self._client, person.person_id)

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


class WindowInference:
    """The inference calls, answered by the host under the command's grant."""

    def __init__(self, client: HostClient) -> None:
        self._client = client

    async def agno(self, person_id: str, call: AgnoCall) -> AgnoAnswer:
        answer = await self._call(
            "agno",
            person_id=person_id,
            # A value JSON cannot carry reaches the prompt as its text.
            call=call.model_dump(mode="json", fallback=str),
        )
        return AgnoAnswer.model_validate(answer)

    async def jev(self, call: JevCall) -> dict[str, Any]:
        return dict(await self._call("jev", call=call.model_dump(mode="json")))

    async def _call(self, name: str, **arguments: Any) -> Any:
        """Preserve the host's safe failure message, including window failures.

        Raises:
            CommandError: If the host reports that the call failed.
            HostCallError: If the window refused the call or is unavailable.
        """
        try:
            return await self._client.acall(name, **arguments)
        except HostCallError as exc:
            if exc.category != "failed":
                raise
            raise CommandError(str(exc)) from exc
