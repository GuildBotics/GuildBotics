from __future__ import annotations

import datetime as dt
import shlex
from typing import Any

from croniter import croniter  # type: ignore[import]

from guildbotics.commands.utils import stringify_output
from guildbotics.integrations.chat_service import ChatService
from guildbotics.integrations.chat_profile import get_chat_scheduled_posts
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ScheduledPostState,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore


async def main(
    context: Any,
    chat_service: ChatService | None = None,
    state_store: ConversationStateStore | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Post scheduled chat messages by running configured custom commands."""
    chat_service = chat_service or context.get_chat_service()
    state_store = state_store or FileConversationStateStore()
    now = now or dt.datetime.now()

    for post in get_chat_scheduled_posts(context.person):
        if not bool(post.get("enabled", True)):
            continue
        service_name = str(post.get("service", "slack")).lower()
        if service_name != "slack":
            continue

        name = str(post.get("name", "")).strip()
        channel_id = await _resolve_post_channel_id(context, chat_service, post)
        cron_expr = str(post.get("cron", "")).strip()
        command_text = str(post.get("command", "")).strip()
        if not (name and channel_id and cron_expr and command_text):
            continue

        if not _is_due_now(cron_expr, now):
            continue

        slot = _minute_slot(now)
        sched_state = state_store.load_scheduled_post_state(
            service_name, context.person.person_id, name
        )
        if sched_state.last_run_slot == slot:
            continue

        text = await _run_command_text(context, command_text)
        if text.strip():
            await chat_service.post_message(channel_id, text)

        sched_state.last_run_slot = slot
        state_store.save_scheduled_post_state(
            service_name, context.person.person_id, name, sched_state
        )

def _is_due_now(cron_expr: str, now: dt.datetime) -> bool:
    minute = now.replace(second=0, microsecond=0)
    prev = minute - dt.timedelta(minutes=1)
    next_time = croniter(cron_expr, prev).get_next(dt.datetime)
    return next_time == minute


def _minute_slot(now: dt.datetime) -> str:
    return now.replace(second=0, microsecond=0).isoformat(timespec="minutes")


async def _run_command_text(context: Any, command_text: str) -> str:
    parts = shlex.split(command_text)
    if not parts:
        return ""
    result = await context.invoke(parts[0], *parts[1:])
    return stringify_output(result)


async def _resolve_post_channel_id(
    context: Any, chat_service: ChatService, post: dict[str, Any]
) -> str:
    channel_id = str(post.get("channel_id", "")).strip()
    if channel_id:
        return channel_id
    channel_name = str(post.get("channel_name", "")).strip()
    if not channel_name:
        return ""
    resolved = await chat_service.resolve_channel_id(channel_name)
    if not resolved:
        _log_info(
            context,
            "chat scheduled post skipped: channel_name=%s could not be resolved "
            "(check channel name, bot channel membership, and Slack scopes "
            "channels:read/groups:read)",
            channel_name,
        )
    return resolved or ""


def _log_info(context: Any, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        logger.info(msg, *args)
    except Exception:
        return
