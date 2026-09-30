"""Repository reads using the configured member's code-hosting service."""

import json
from typing import Any

from guildbotics.integrations.code_hosting_service import (
    MAX_PAGE_BYTES,
    RepositoryReadError,
)
from guildbotics.runtime.context import Context
from guildbotics.utils.i18n_tool import t


async def read_repository(
    context: Context,
    resource: str,
    repo: str,
    *,
    identifier: str,
    parameters: dict[str, Any],
    continuation: str,
) -> dict[str, Any]:
    service = context.integration_factory.create_code_hosting_service(
        context.logger, context.person, context.team
    )
    try:
        page = await service.read(
            resource,
            repo,
            identifier=identifier,
            parameters=parameters,
            continuation=continuation,
        )
        result = page.model_dump()
        if len(json.dumps(result).encode()) > MAX_PAGE_BYTES:
            raise RepositoryReadError(t("integrations.repository.too_large"))
        return result
    finally:
        await service.aclose()
