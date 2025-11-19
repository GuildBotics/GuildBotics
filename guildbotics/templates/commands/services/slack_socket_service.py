from __future__ import annotations

import asyncio
import shlex
from typing import Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from guildbotics.drivers.utils import run_command
from guildbotics.runtime.context import Context

DEFAULT_WORKFLOW = "workflows/ticket_driven_workflow"


async def _ack(client: AsyncBaseSocketModeClient, envelope_id: str | None) -> None:
    if envelope_id:
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=envelope_id)
        )


async def _kickoff_command(
    base_context: Context, *, command: str, pipe: str, task_type: str
) -> None:
    """Fire-and-forget command execution based on Slack input."""
    ctx = base_context.clone_for(base_context.person)
    ctx.pipe = pipe
    ok = await run_command(ctx, command, task_type)
    if not ok:
        ctx.logger.warning(
            f"[Slack] command '{command}' failed (task_type={task_type})"
        )


async def _handle_request(
    context: Context,
    client: AsyncBaseSocketModeClient,
    req: SocketModeRequest,
    workflow: str,
) -> None:
    await _ack(client, getattr(req, "envelope_id", None))
    payload: dict[str, Any] = req.payload or {}
    event = payload.get("event") or {}
    event_type = event.get("type") or req.type
    context.logger.info(
        f"[Slack] Received event type={event_type} team={payload.get('team_id')}"
    )

    # Slash commands
    if req.type == "slash_commands":
        command_name = str(payload.get("command", "")).lstrip("/")
        text = payload.get("text") or ""
        args = shlex.split(text) if text else []
        command_str = " ".join([command_name] + args).strip()
        if command_name:
            asyncio.create_task(
                _kickoff_command(
                    context,
                    command=command_str,
                    pipe=text,
                    task_type="slack_slash",
                )
            )
        return

    # Events API (message/app_mention)
    if req.type == "events_api" and event_type in {"message", "app_mention"}:
        # Ignore bot/self messages to prevent loops
        subtype = event.get("subtype")
        if subtype in {"bot_message", "message_deleted", "message_changed"}:
            return
        text = event.get("text") or ""
        command_name = workflow or DEFAULT_WORKFLOW
        asyncio.create_task(
            _kickoff_command(
                context, command=command_name, pipe=text, task_type="slack_message"
            )
        )


async def main(context: Context, workflow: str | None = None) -> str:
    """
    Maintain a Slack Socket Mode connection for this person.
    Requires person-scoped env vars: SLACK_APP_TOKEN (xapp-), SLACK_BOT_TOKEN (xoxb-).
    """
    stop_event = context.shutdown_event
    try:
        app_token = context.person.get_secret("SLACK_APP_TOKEN")
        bot_token = context.person.get_secret("SLACK_BOT_TOKEN")
    except KeyError as exc:
        context.logger.error(f"[Slack] Missing token: {exc}")
        return "slack socket: missing token"

    web_client = AsyncWebClient(token=bot_token, logger=context.logger)
    client = SocketModeClient(
        app_token=app_token, web_client=web_client, logger=context.logger
    )

    async def _on_request(
        socket_client: AsyncBaseSocketModeClient, req: SocketModeRequest
    ) -> None:
        resolved_workflow = workflow or DEFAULT_WORKFLOW
        await _handle_request(context, socket_client, req, resolved_workflow)

    client.socket_mode_request_listeners.append(_on_request)

    try:
        await client.connect()
        context.logger.info("[Slack] Socket Mode client connected.")
        while stop_event is None or not stop_event.is_set():
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        raise
    finally:
        await client.close()
        session = getattr(web_client, "session", None)
        if session is not None:
            await session.close()
        context.logger.info("[Slack] Socket Mode client disconnected.")

    return "slack socket: stopped"
