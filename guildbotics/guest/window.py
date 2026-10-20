"""The member's services and the external inference calls, as a command
inside its isolated environment reaches them.

The member's credentials never leave the host, so the services a command's
code uses (:class:`ChatService`, :class:`CodeHostingService`,
:class:`TicketService`) are the member's own commands, run by the host
through the command's window as an agent's are: the command can do with them
only what may be handed to an agent. The
external inference calls (:class:`WindowInference`) go to the host the same
way, where the keys are.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from logging import Logger
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.entities import Person, Task, Team
from guildbotics.guest.host_client import HostClient
from guildbotics.intelligences.agent_runtime.wire import HostCallError
from guildbotics.intelligences.brains.inference import AgnoAnswer, AgnoCall, JevCall
from guildbotics.runtime.chat_service import (
    ChatEventPage,
    ChatIdentity,
    ChatMessageRef,
    ChatPostResult,
    ChatService,
    CredentialCheck,
)
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.integration_factory import IntegrationFactory
from guildbotics.runtime.port import ServiceError, WorkflowStatus
from guildbotics.runtime.resources import (
    RESOURCE_SUBJECTS,
    CommentWritten,
    ReactionAdded,
    ReadPage,
    ReadSubject,
)
from guildbotics.runtime.ticket_service import (
    ClosedItem,
    MoveLane,
    TicketCreated,
    TicketService,
    TicketUpdated,
)
from guildbotics.utils.i18n_tool import t


class MemberCommandError(RuntimeError):
    """A member command the window ran failed; the message is its error."""


#: The member command group that reads each subject's resources.
_READ_GROUPS: dict[ReadSubject, str] = {
    "ticket": "ticket",
    "pull_request": "pr",
    "repository": "repo-info",
}


async def _read(
    client: HostClient,
    person_id: str,
    resource: str,
    subject: str,
    parameters: dict[str, Any] | None,
    continuation: str,
) -> ReadPage:
    """A page of ``resource``, read by the member's read command of it."""
    if resource not in RESOURCE_SUBJECTS:
        raise ServiceError(t("integrations.resources.resource"))
    result = await _member_result(
        client,
        [
            _READ_GROUPS[RESOURCE_SUBJECTS[resource]],
            "read",
            "--person",
            person_id,
            "--resource",
            resource,
            "--subject",
            subject,
            "--params",
            json.dumps(parameters if parameters is not None else {}),
            "--continuation",
            continuation,
            "--format",
            "json",
        ],
        failure=ServiceError,
    )
    page = ReadPage.model_validate(result)
    # What the host answered is held to the resource's schema here too: a
    # command reads each item in its full shape, defaults included.
    return ReadPage.of(
        resource, page.items, continuation=page.continuation, target=page.target
    )


class WindowCodeHostingService(CodeHostingService):
    """The code host's reads, via the command's member grant."""

    def __init__(self, client: HostClient, person_id: str) -> None:
        self._client = client
        self._person_id = person_id

    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        return await _read(
            self._client, self._person_id, resource, subject, parameters, continuation
        )

    async def aclose(self) -> None:
        """The command owns the shared host client."""


class WindowTicketService(TicketService):
    """The tickets' reads, via the command's member grant. Their writes and
    the board are the host's: each is the host's ticket selection, or has no
    member command that does the same."""

    def __init__(self, client: HostClient, person: Person, team: Team) -> None:
        super().__init__(logging.getLogger(__name__), person, team)
        self._client = client

    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        return await _read(
            self._client,
            self.person.person_id,
            resource,
            subject,
            parameters,
            continuation,
        )

    async def comment(self, url: str, body: str, **_: Any) -> CommentWritten:
        raise _unavailable("Commenting on a ticket")

    async def create_ticket(self, tracker: str, *_: Any, **__: Any) -> TicketCreated:
        raise _unavailable("Creating a ticket")

    async def update_ticket(self, url: str, **_: Any) -> TicketUpdated:
        raise _unavailable("Updating a ticket")

    async def add_reaction(self, url: str, *_: Any) -> ReactionAdded:
        raise _unavailable("Reacting on a ticket")

    async def candidates(self) -> list[Task]:
        raise _unavailable("The board's candidates")

    async def refresh(self, task: Task) -> Task | None:
        raise _unavailable("The board's candidates")

    async def move(self, task: Task, lane: MoveLane) -> bool:
        raise _unavailable("Moving a ticket")

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        raise _unavailable("The board's closed work")

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
        status: WorkflowStatus | None = None,
    ) -> ChatPostResult:
        if status is not None:
            raise _unavailable("Posting a workflow status")
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
            failure=ServiceError,
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

    def create_ticket_service(
        self, logger: Logger, person: Person, team: Team
    ) -> TicketService:
        return WindowTicketService(self._client, person, team)

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
