"""Display the host's readiness result without reimplementing its rules."""

from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.commands.repository import RepositoryReader, display, flag
from guildbotics.runtime.code_hosting_service import (
    MAX_LOG_TAIL_BYTES,
    ReadinessQuery,
)
from guildbotics.utils.i18n_tool import t

COMMAND_METADATA = {
    "description": {
        "en": "Check pull-request CI and readiness, with optional failed log tails.",
        "ja": "PR の CI・完了可否と、必要に応じて失敗ログを確認します。",
    },
    "read_only": True,
}


async def main(
    context: Any,
    repo: str,
    number: str,
    failed_logs: str = "false",
    log_tail_bytes: str = str(ReadinessQuery.model_fields["log_tail_bytes"].default),
) -> str:
    try:
        size = int(log_tail_bytes)
        ReadinessQuery(log_tail_bytes=size)
    except ValueError:
        raise CommandError(
            t("commands.repository.inspect.log_size", maximum=MAX_LOG_TAIL_BYTES)
        ) from None
    return display(
        await RepositoryReader(context, repo, number).one(
            "pull_request_readiness",
            failed_logs=flag(failed_logs),
            log_tail_bytes=size,
        )
    )
