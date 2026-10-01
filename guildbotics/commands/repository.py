"""Collect bounded repository pages for bundled inspection commands."""

import json
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.integrations.code_hosting_service import RepositoryReadError
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT


class RepositoryReader:
    """A single inspection's aggregate bound, shared by all of its resources."""

    def __init__(self, context: Any, repo: str, number: str) -> None:
        self.service = context.get_code_hosting_service()
        self.repo = repo
        self.number = str(number)
        self.bytes = 0

    async def read(self, resource: str, **parameters: Any) -> list[dict[str, Any]]:
        continuation = ""
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        try:
            for _ in range(100):
                page = await self.service.read(
                    resource,
                    self.repo,
                    identifier=self.number,
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
        except RepositoryReadError as exc:
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
