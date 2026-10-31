"""What the member capabilities read of a ``Context``, for their tests."""

from __future__ import annotations

import logging
import io
from dataclasses import dataclass
from pathlib import Path

from guildbotics.entities.team import Person, Team
from guildbotics.runtime.code_hosting_service import CodeHostingService
from guildbotics.runtime.member_invocation import (
    GuestResult,
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.runtime.ticket_service import TicketService


@dataclass
class MemberServices:
    """The member, the team, and the services a capability is handed."""

    person: Person
    team: Team
    code: CodeHostingService
    tickets: TicketService | None = None
    logger: logging.Logger = logging.getLogger("tests.member_services")

    def get_code_hosting_service(self) -> CodeHostingService:
        return self.code

    def get_ticket_service(self) -> TicketService:
        assert self.tickets is not None, "no ticket service in this test"
        return self.tickets

    async def aclose(self) -> None:
        await self.code.aclose()
        if self.tickets is not None:
            await self.tickets.aclose()


class GuestDouble:
    """A command's environment as a member command sees it: what it was
    asked to run, and what it reads on its standard input."""

    def __init__(self, result: GuestResult | Exception) -> None:
        self.result = result
        self.runs: list[dict[str, object]] = []

    def path(self, host: Path) -> str:
        return f"/guest{host}"

    def python(self, module: str, *args: str) -> list[str]:
        return ["python", "-m", module, *args]

    def remaining(self) -> float:
        return 60.0

    def run(self, argv, *, cwd, env, stdin=b"", stdout=None, stdout_limit):
        self.runs.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": env,
                "stdout_limit": stdout_limit,
                "stdin": stdin,
                "read": stdin.read_bytes(),
            }
        )
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def download_on_client(operation):
    from guildbotics.capabilities.artifact_archive import (
        write_download,
        unpacked,
        ArtifactError,
    )

    buffer = io.BytesIO()
    received = {}

    def receive(source, destination, attachment):
        buffer.write(source.read())
        received.update(destination=destination, attachment=attachment)

    with member_invocation_scope(MemberInvocation(download=receive)):
        result = await operation
    buffer.seek(0)
    destination = Path(received["destination"])
    try:
        files = write_download(buffer, destination, *received["attachment"])
    except ArtifactError as exc:
        raise MemberCapabilityError(str(exc)) from exc
    return {**result, **unpacked(destination, files)}
