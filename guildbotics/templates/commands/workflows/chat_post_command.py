from __future__ import annotations

import shlex
from typing import Any

from guildbotics.commands.utils import stringify_output
from guildbotics.integrations.chat_service import ChatService


async def main(
    context: Any,
    *,
    service: str = "slack",
    channel_id: str = "",
    channel_name: str = "",
    command: str = "",
) -> str:
    """Run a GuildBotics command and post its output to a chat channel."""
    service_name = str(service).strip().lower()
    if service_name != "slack":
        raise ValueError(f"Unsupported chat service: {service}")

    chat_service = context.get_chat_service()
    resolved_channel_id = await _resolve_channel_id(
        context,
        chat_service,
        channel_id=str(channel_id or "").strip(),
        channel_name=str(channel_name or "").strip(),
    )
    if not resolved_channel_id:
        return ""

    command_text = str(command or "").strip()
    if not command_text:
        return ""

    text = await _run_command_text(context, command_text)
    if not text.strip():
        return ""

    await chat_service.post_message(resolved_channel_id, text)
    return text


async def _resolve_channel_id(
    context: Any,
    chat_service: ChatService,
    *,
    channel_id: str,
    channel_name: str,
) -> str:
    if channel_id:
        return channel_id
    if not channel_name:
        _log_info(context, "chat_post_command skipped: channel_id or channel_name is required")
        return ""
    resolved = await chat_service.resolve_channel_id(channel_name)
    if not resolved:
        _log_info(
            context,
            "chat_post_command skipped: channel_name=%s could not be resolved "
            "(check channel name, bot channel membership, and Slack scopes "
            "channels:read/groups:read)",
            channel_name,
        )
        return ""
    return resolved


async def _run_command_text(context: Any, command_text: str) -> str:
    try:
        parts = shlex.split(command_text)
    except ValueError as e:
        _log_info(
            context,
            "chat_post_command skipped: invalid command syntax (check quotes): %s",
            e,
        )
        return ""
    if not parts:
        return ""
    result = await context.invoke(parts[0], *parts[1:])
    return stringify_output(result)


def _log_info(context: Any, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        logger.info(msg, *args)
    except Exception:
        return
