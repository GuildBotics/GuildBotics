"""Collect the bounded pages of a ticket's, a pull request's, or a
repository's resources for the bundled inspection commands."""

import json
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import TICKET_RESOURCES
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT


class RepositoryReader:
    """A single inspection's aggregate bound, shared by all of its resources,
    which are of ``subject``: a ticket's or pull request's URL, or a
    repository's path."""

    def __init__(self, context: Any, subject: str) -> None:
        self.context = context
        self.subject = subject
        self.bytes = 0

    async def read(self, resource: str, **parameters: Any) -> list[dict[str, Any]]:
        continuation = ""
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        service = (
            self.context.get_ticket_service()
            if resource in TICKET_RESOURCES
            else self.context.get_code_hosting_service()
        )
        try:
            for _ in range(100):
                page = await service.read(
                    resource,
                    self.subject,
                    parameters=parameters,
                    continuation=continuation,
                )
                self.bytes += len(page.model_dump_json().encode())
                if self.bytes > STREAM_READ_LIMIT // 4:
                    break
                items.extend(page.model_dump()["items"])
                if not page.continuation:
                    return items
                if page.continuation in seen:
                    break
                continuation = page.continuation
                seen.add(continuation)
        except ServiceError as exc:
            raise CommandError(str(exc)) from exc
        raise CommandError(
            t("commands.repository.inspect.incomplete", resource=resource)
        )

    async def one(self, resource: str, **parameters: Any) -> dict[str, Any]:
        items = await self.read(resource, **parameters)
        if len(items) != 1:
            raise CommandError(
                t("commands.repository.inspect.incomplete", resource=resource)
            )
        return items[0]


def flag(value: str) -> bool:
    if value not in {"true", "false"}:
        raise CommandError(t("commands.repository.inspect.flag"))
    return value == "true"


def display(result: dict[str, Any]) -> str:
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if len(rendered.encode()) > STREAM_READ_LIMIT // 2:
        raise CommandError(
            t("commands.repository.inspect.incomplete", resource="inspection")
        )
    return rendered
