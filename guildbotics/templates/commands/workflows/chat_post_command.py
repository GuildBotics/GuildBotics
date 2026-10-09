from __future__ import annotations

import shlex
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.commands.utils import stringify_output
from guildbotics.runtime.chat_service import ChatService, ChatServiceError
from guildbotics.utils.i18n_tool import t


async def main(
    context: Any,
    *,
    service: str = "slack",
    channel_id: str = "",
    channel_name: str = "",
    command: str = "",
) -> str:
    """Run a GuildBotics command and post its output to a chat channel.

    Raises:
        CommandError: If the service is unsupported, the channel cannot be
            resolved, the command cannot be split, or chat reports a failure.
            An empty command output is not a failure: nothing is posted.
    """
    service_name = str(service).strip().lower()
    if service_name != "slack":
        raise CommandError(
            t(
                "commands.workflows.chat_post_command.unsupported_service",
                service=service,
            )
        )

    chat_service = context.get_chat_service()
    resolved_channel_id = await _resolve_channel_id(
        chat_service,
        channel_id=str(channel_id or "").strip(),
        channel_name=str(channel_name or "").strip(),
    )
    try:
        parts = shlex.split(str(command or ""))
    except ValueError as exc:
        raise CommandError(f"Invalid command syntax: {exc}") from exc
    if not parts:
        raise CommandError("A command to post is required.")

    text = stringify_output(await context.invoke(parts[0], *parts[1:]))
    if not text.strip():
        return ""

    try:
        await chat_service.post_message(resolved_channel_id, text)
    except ChatServiceError as exc:
        raise CommandError(str(exc)) from exc
    return text


async def _resolve_channel_id(
    chat_service: ChatService, *, channel_id: str, channel_name: str
) -> str:
    if channel_id:
        return channel_id
    if not channel_name:
        raise CommandError("Either channel_id or channel_name is required.")
    try:
        resolved = await chat_service.resolve_channel_id(channel_name)
    except ChatServiceError as exc:
        raise CommandError(str(exc)) from exc
    if not resolved:
        raise CommandError(
            f"Chat channel was not found: {channel_name} (check the channel "
            "name, the bot's channel membership, and the Slack scopes "
            "channels:read/groups:read)"
        )
    return resolved
