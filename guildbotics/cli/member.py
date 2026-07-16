from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import click

from guildbotics.capabilities.member_activity_events import (
    record_member_issue_comment_event,
    record_member_issue_create_event,
    record_member_pr_create_event,
    record_member_push_event,
)
from guildbotics.capabilities.member_chat import MemberChatCapabilityService
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    MemberCapabilityError,
    MemberGitHubCapabilityService,
)
from guildbotics.capabilities.member_memory import (
    MemberMemoryError,
    MemberMemoryService,
)
from guildbotics.capabilities.member_reference import (
    capability_reference_text,
    command_summary,
)
from guildbotics.capabilities.task_runs import (
    RUN_ENV,
    TASK_RUN_ENV,
    RunStore,
    TaskRunError,
    TaskRunStore,
    current_run_id,
    current_task_run_id,
)
from guildbotics.cli._options import format_option
from guildbotics.commands.errors import (
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
)
from guildbotics.observability import trace_scope
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.interactive_sessions import (
    InteractiveTraceSession,
    InteractiveTraceStore,
    interactive_host,
    interactive_thread_key,
)
from guildbotics.runtime.member_context import resolve_member_context
from guildbotics.utils.env_loader import load_guildbotics_env
from guildbotics.utils.fileio import get_workspace_data_root
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.workspace_state import apply_workspace_for_cli

WorkspaceMode = Literal["member", "current"]
SLACK_TS_FRACTION_DIGITS = 6
_READ_ONLY_COMMAND_ATTRIBUTE = "__guildbotics_member_read_only__"


def _read_only_member_command(
    callback: Callable[..., Any],
) -> Callable[..., Any]:
    """Declare that a member command cannot mutate local or remote state."""
    setattr(callback, _READ_ONLY_COMMAND_ATTRIBUTE, True)
    return callback


_person_option = click.option(
    "--person", required=True, help="Person ID or name of the member."
)
_json_format_option = format_option("json")
_markdown_format_option = format_option("markdown")
_service_option = click.option(
    "--service",
    "service_name",
    type=click.Choice(["slack"]),
    default="slack",
    help="Chat service to use.",
)
_workspace_mode_option = click.option(
    "--workspace-mode",
    type=click.Choice(["member", "current"]),
    default="member",
    help=(
        "Use 'member' for isolated workflow workspaces or 'current' for the "
        "repository currently open in an interactive coding session."
    ),
)
_required_content_stdin_option = click.option(
    "--content-stdin",
    is_flag=True,
    required=True,
    expose_value=False,
    help="Read the command's entire free-form content from standard input.",
)


@click.group()
@click.pass_context
@click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Workspace root to use instead of the persisted active workspace.",
)
def member(ctx: click.Context, workspace_dir: Path | None) -> None:
    """Operate as a configured GuildBotics member."""
    try:
        applied_workspace = apply_workspace_for_cli(workspace_dir)
    except NotADirectoryError as exc:
        raise click.ClickException(f"workspace does not exist: {exc}") from exc
    if applied_workspace is not None:
        load_guildbotics_env(
            applied_workspace.workspace,
            override=False,
            prefer_env_file=False,
        )
    else:
        load_guildbotics_env(Path.cwd(), override=False, prefer_env_file=True)
    ctx.obj = {
        "workspace": str(
            (applied_workspace.workspace if applied_workspace else Path.cwd()).resolve()
        )
    }


@member.command(name="context")
@_read_only_member_command
@_person_option
@click.option(
    "--check-credentials",
    is_flag=True,
    help="Also verify the member's provider credentials.",
)
@_markdown_format_option
def context_cmd(person: str, check_credentials: bool, output_format: str) -> None:
    """Show non-secret member context."""
    _run(
        _context_cmd(person, check_credentials, output_format),
        output_format=output_format,
    )


@member.command(name="help")
@_read_only_member_command
def help_cmd() -> None:
    """Print the member capability reference (commands and cross-cutting rules).

    This is the same reference embedded in ``member context``; use it to reread
    the available commands without re-running the full context.
    """
    click.echo(capability_reference_text())


@member.group(name="agent", help=t("cli.member.agent.help"))
def agent() -> None:
    """Manage native agent runtime state."""


@agent.group(name="conversation", help=t("cli.member.agent.conversation_help"))
def agent_conversation() -> None:
    """Manage persisted native agent conversations."""


@agent_conversation.command(
    name="reset", help=t("cli.member.agent_conversation_reset.help")
)
@_person_option
@click.option(
    "--adapter",
    type=click.Choice(["codex", "claude"]),
    required=True,
    help=t("cli.member.agent_conversation_reset.adapter_help"),
)
@click.option(
    "--work-kind",
    type=click.Choice(["ticket", "chat", "manual"]),
    required=True,
    help=t("cli.member.agent_conversation_reset.work_kind_help"),
)
@click.option(
    "--work-identity",
    required=True,
    help=t("cli.member.agent_conversation_reset.work_identity_help"),
)
@_json_format_option
def agent_conversation_reset(
    person: str,
    adapter: str,
    work_kind: str,
    work_identity: str,
    output_format: str,
) -> None:
    """Reset one exact native provider session without deleting history."""
    _run(
        _agent_conversation_reset(person, adapter, work_kind, work_identity),
        output_format=output_format,
    )


async def _agent_conversation_reset(
    person: str, adapter: str, work_kind: str, work_identity: str
) -> dict[str, Any]:
    from guildbotics.intelligences.agent_runtime.models import (
        ConversationKey,
        ResumePolicy,
    )
    from guildbotics.intelligences.agent_runtime.store import ConversationStore

    _context, member_person = _resolve(person)
    key = ConversationKey(
        person_id=member_person.person_id,
        adapter=adapter,
        work_kind=work_kind,
        work_identity=work_identity,
    )
    store = ConversationStore(get_workspace_data_root())
    record = store.resolve(key, ResumePolicy.RESET)
    store.save(record)
    return {
        "person_id": member_person.person_id,
        "adapter": adapter,
        "work_kind": work_kind,
        "work_identity": work_identity,
        "generation": record.generation,
        "reset": True,
    }


@member.group()
def memory() -> None:
    """Record, recall, and maintain member memory documents."""


@memory.command(name="record")
@_person_option
@click.option(
    "--scope",
    type=click.Choice(["personal", "team"]),
    default="personal",
    help="Store as personal or team memory.",
)
@click.option("--title", required=True, help="Document title.")
@click.option(
    "--summary",
    default="",
    help="One-line summary shown in recall hits and the digest.",
)
@click.option(
    "--keyword",
    "keywords",
    multiple=True,
    help="Recall keyword. May be repeated.",
)
@click.option(
    "--ticket",
    "tickets",
    multiple=True,
    help="Related ticket URL (source anchor). May be repeated.",
)
@click.option(
    "--pr",
    "prs",
    multiple=True,
    help="Related PR URL (source anchor). May be repeated.",
)
@click.option(
    "--channel",
    "channels",
    multiple=True,
    help="Related chat channel URL (source anchor). May be repeated.",
)
@click.option(
    "--thread",
    "threads",
    multiple=True,
    help="Related chat thread URL (source anchor). May be repeated.",
)
@click.option(
    "--kind",
    type=click.Choice(["note", "policy"]),
    default="note",
    help="Document kind; 'policy' requires --policy-approved.",
)
@click.option(
    "--pin",
    "pinned",
    is_flag=True,
    help="Pin as a standing rule included in member context.",
)
@_required_content_stdin_option
@click.option(
    "--policy-approved",
    is_flag=True,
    help="Confirm that a human approved this policy memory change.",
)
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="Extra metadata as key=value. May be repeated.",
)
@_json_format_option
def memory_record(
    person: str,
    scope: str,
    title: str,
    summary: str,
    keywords: tuple[str, ...],
    tickets: tuple[str, ...],
    prs: tuple[str, ...],
    channels: tuple[str, ...],
    threads: tuple[str, ...],
    kind: str,
    pinned: bool,
    policy_approved: bool,
    set_values: tuple[str, ...],
    output_format: str,
) -> None:
    title = _validate_title(title)
    body = _read_stdin("memory body")
    _run(
        _memory_record(
            person,
            scope,
            title,
            summary,
            list(keywords),
            _source_entries(tickets, prs, channels, threads),
            kind,
            pinned,
            body,
            policy_approved,
            _parse_set_values(set_values),
        ),
        output_format=output_format,
    )


async def _memory_record(
    person: str,
    scope: str,
    title: str,
    summary: str,
    keywords: list[str],
    source: list[dict[str, Any]],
    kind: str,
    pinned: bool,
    body: str,
    policy_approved: bool,
    params: dict[str, Any],
) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).record(
        scope=cast(Any, scope),
        title=title,
        summary=summary,
        keywords=keywords,
        source=source,
        kind=kind,
        pinned=pinned,
        body=body,
        policy_approved=policy_approved,
        params=params,
    )


@memory.command(name="recall")
@_read_only_member_command
@_person_option
@click.option(
    "--query",
    "queries",
    multiple=True,
    help="Literal search query; repeat for OR matching.",
)
@click.option(
    "--meta-only",
    is_flag=True,
    help="Return hit metadata without body excerpts.",
)
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=20,
    help="Maximum number of hits.",
)
@_json_format_option
def memory_recall(
    person: str,
    queries: tuple[str, ...],
    meta_only: bool,
    limit: int,
    output_format: str,
) -> None:
    _run(
        _memory_recall(person, list(queries), meta_only, limit),
        output_format=output_format,
    )


async def _memory_recall(
    person: str, queries: list[str], meta_only: bool, limit: int
) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).recall(
        queries=queries,
        meta_only=meta_only,
        limit=limit,
    )


@memory.command(name="get")
@_read_only_member_command
@_person_option
@click.option("--id", "doc_id", required=True, help="Memory document id.")
@click.option(
    "--team",
    "team_scope",
    is_flag=True,
    help="Operate on team memory instead of personal memory.",
)
@_json_format_option
def memory_get(person: str, doc_id: str, team_scope: bool, output_format: str) -> None:
    _run(
        _memory_get(person, doc_id, "team" if team_scope else None),
        output_format=output_format,
    )


async def _memory_get(person: str, doc_id: str, scope: str | None) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).get(doc_id=doc_id, scope=cast(Any, scope))


@memory.command(name="update")
@_person_option
@click.option("--id", "doc_id", required=True, help="Memory document id.")
@click.option(
    "--team",
    "team_scope",
    is_flag=True,
    help="Operate on team memory instead of personal memory.",
)
@click.option("--title", help="New document title.")
@click.option("--summary", help="New one-line summary.")
@click.option(
    "--keyword",
    "keywords",
    multiple=True,
    help="Replace all recall keywords. May be repeated.",
)
@click.option(
    "--add-keyword",
    "add_keywords",
    multiple=True,
    help="Add a recall keyword. May be repeated.",
)
@click.option(
    "--remove-keyword",
    "remove_keywords",
    multiple=True,
    help="Remove a recall keyword. May be repeated.",
)
@click.option(
    "--ticket",
    "tickets",
    multiple=True,
    help="Related ticket URL (source anchor). May be repeated.",
)
@click.option(
    "--pr",
    "prs",
    multiple=True,
    help="Related PR URL (source anchor). May be repeated.",
)
@click.option(
    "--channel",
    "channels",
    multiple=True,
    help="Related chat channel URL (source anchor). May be repeated.",
)
@click.option(
    "--thread",
    "threads",
    multiple=True,
    help="Related chat thread URL (source anchor). May be repeated.",
)
@click.option(
    "--pin",
    "pin_value",
    flag_value=True,
    default=None,
    help="Pin as a standing rule included in member context.",
)
@click.option("--unpin", "pin_value", flag_value=False, help="Remove the pin.")
@click.option(
    "--kind",
    type=click.Choice(["note", "policy"]),
    help="Change the document kind; 'policy' requires --policy-approved.",
)
@click.option(
    "--content-stdin",
    is_flag=True,
    help="Read the entire document body from standard input.",
)
@click.option(
    "--policy-approved",
    is_flag=True,
    help="Confirm that a human approved this policy memory change.",
)
@click.option(
    "--set",
    "set_values",
    multiple=True,
    help="Extra metadata as key=value. May be repeated.",
)
@_json_format_option
def memory_update(
    person: str,
    doc_id: str,
    team_scope: bool,
    title: str | None,
    summary: str | None,
    keywords: tuple[str, ...],
    add_keywords: tuple[str, ...],
    remove_keywords: tuple[str, ...],
    tickets: tuple[str, ...],
    prs: tuple[str, ...],
    channels: tuple[str, ...],
    threads: tuple[str, ...],
    pin_value: bool | None,
    kind: str | None,
    content_stdin: bool,
    policy_approved: bool,
    set_values: tuple[str, ...],
    output_format: str,
) -> None:
    body = _read_optional_stdin(content_stdin, "memory body")
    if title is not None:
        title = _validate_title(title)
    source = _source_entries(tickets, prs, channels, threads)
    _run(
        _memory_update(
            person=person,
            doc_id=doc_id,
            scope="team" if team_scope else None,
            title=title,
            summary=summary,
            keywords=list(keywords) if keywords else None,
            add_keywords=list(add_keywords),
            remove_keywords=list(remove_keywords),
            source=source if source else None,
            pinned=pin_value,
            kind=kind,
            body=body,
            policy_approved=policy_approved,
            params=_parse_set_values(set_values),
        ),
        output_format=output_format,
    )


async def _memory_update(**kwargs: Any) -> dict[str, Any]:
    _context, member_person = _resolve(str(kwargs.pop("person")))
    return MemberMemoryService(member_person).update(**kwargs)


@memory.command(name="touch")
@_person_option
@click.option("--id", "doc_id", required=True, help="Memory document id.")
@click.option(
    "--team",
    "team_scope",
    is_flag=True,
    help="Operate on team memory instead of personal memory.",
)
@_json_format_option
def memory_touch(
    person: str, doc_id: str, team_scope: bool, output_format: str
) -> None:
    _run(
        _memory_touch(person, doc_id, "team" if team_scope else None),
        output_format=output_format,
    )


async def _memory_touch(person: str, doc_id: str, scope: str | None) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).touch(
        doc_id=doc_id, scope=cast(Any, scope)
    )


@memory.command(name="archive")
@_person_option
@click.option("--id", "doc_id", required=True, help="Memory document id.")
@click.option(
    "--team",
    "team_scope",
    is_flag=True,
    help="Operate on team memory instead of personal memory.",
)
@click.option(
    "--policy-approved",
    is_flag=True,
    help="Confirm that a human approved this policy memory change.",
)
@_json_format_option
def memory_archive(
    person: str,
    doc_id: str,
    team_scope: bool,
    policy_approved: bool,
    output_format: str,
) -> None:
    _run(
        _memory_archive(
            person, doc_id, "team" if team_scope else None, policy_approved
        ),
        output_format=output_format,
    )


async def _memory_archive(
    person: str, doc_id: str, scope: str | None, policy_approved: bool
) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).archive(
        doc_id=doc_id,
        scope=cast(Any, scope),
        policy_approved=policy_approved,
    )


@memory.command(name="promote")
@_person_option
@click.option("--id", "doc_id", required=True, help="Memory document id.")
@_json_format_option
def memory_promote(person: str, doc_id: str, output_format: str) -> None:
    _run(_memory_promote(person, doc_id), output_format=output_format)


async def _memory_promote(person: str, doc_id: str) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).promote(doc_id=doc_id)


async def _context_cmd(
    person: str, check_credentials: bool, output_format: str
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    if member_person.person_type == "human":
        raise click.ClickException(
            str(PersonExecutionNotAllowedError(member_person.person_id))
        )
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.context(check_credentials=check_credentials)
    finally:
        await service.aclose()
    if check_credentials and (
        member_person.has_secret("SLACK_BOT_TOKEN")
        or member_person.has_secret("SLACK_APP_TOKEN")
    ):
        # Build the chat service only when a bot token exists; the factory raises
        # without one. The app-level token is validated independently (it does not
        # need the chat service), so an app-token-only member is still checked.
        chat_service = (
            context.get_chat_service()
            if member_person.has_secret("SLACK_BOT_TOKEN")
            else None
        )
        chat = MemberChatCapabilityService(
            member_person,
            context.team,
            context.logger,
            chat_service,
        )
        try:
            result["chat_credentials"] = await chat.check_credentials()
        finally:
            await chat.aclose()
    return result


@member.group()
def chat() -> None:
    """Chat identity, posting, replies, reactions, and run completion."""


@chat.command(name="identity")
@_read_only_member_command
@_person_option
@_service_option
@_markdown_format_option
def chat_identity(person: str, service_name: str, output_format: str) -> None:
    _run(_chat_identity(person, service_name), output_format=output_format)


async def _chat_identity(person: str, service_name: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        return await service.identity()
    finally:
        await service.aclose()


@chat.group(name="inspect")
def chat_inspect() -> None:
    """Inspect Slack channel or thread messages for interactive decisions."""


@chat_inspect.command(name="channel")
@_read_only_member_command
@_person_option
@_service_option
@click.option("--channel-id", default="", help="Channel id of the target channel.")
@click.option(
    "--channel-name",
    default="",
    help="Channel name (alternative to --channel-id).",
)
@click.option(
    "--oldest-ts",
    default="",
    help="Only include messages at or after this timestamp.",
)
@click.option(
    "--latest-ts",
    default="",
    help="Only include messages at or before this timestamp.",
)
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=50,
    help="Maximum number of messages.",
)
@_json_format_option
def chat_inspect_channel(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    oldest_ts: str,
    latest_ts: str,
    limit: int,
    output_format: str,
) -> None:
    _run(
        _chat_inspect_channel(
            person,
            service_name,
            channel_id or None,
            channel_name or None,
            oldest_ts or None,
            latest_ts or None,
            limit,
        ),
        output_format=output_format,
    )


async def _chat_inspect_channel(
    person: str,
    service_name: str,
    channel_id: str | None,
    channel_name: str | None,
    oldest_ts: str | None,
    latest_ts: str | None,
    limit: int,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        return await service.inspect_channel(
            channel_id=channel_id,
            channel_name=channel_name,
            oldest_ts=oldest_ts,
            latest_ts=latest_ts,
            limit=limit,
        )
    finally:
        await service.aclose()


@chat_inspect.command(name="thread")
@_read_only_member_command
@_person_option
@_service_option
@click.option("--channel-id", default="", help="Channel id of the target channel.")
@click.option(
    "--channel-name",
    default="",
    help="Channel name (alternative to --channel-id).",
)
@click.option("--thread-ts", default="", help="Thread timestamp (with --channel-id).")
@click.option(
    "--message-url",
    default="",
    help="Slack message URL (alternative to channel/timestamp options).",
)
@click.option(
    "--limit",
    type=click.IntRange(1, 200),
    default=100,
    help="Maximum number of messages.",
)
@_json_format_option
def chat_inspect_thread(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    thread_ts: str,
    message_url: str,
    limit: int,
    output_format: str,
) -> None:
    ref = _resolve_message_reference(
        channel_id=channel_id or None,
        thread_ts=thread_ts or None,
        message_ts=None,
        message_url=message_url or None,
    )
    resolved_thread_ts = ref["thread_ts"] or ref["message_ts"]
    if not resolved_thread_ts:
        raise click.ClickException("Either --thread-ts or --message-url is required.")
    _run(
        _chat_inspect_thread(
            person,
            service_name,
            ref["channel_id"],
            channel_name or None,
            resolved_thread_ts,
            limit,
        ),
        output_format=output_format,
    )


async def _chat_inspect_thread(
    person: str,
    service_name: str,
    channel_id: str | None,
    channel_name: str | None,
    thread_ts: str,
    limit: int,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        return await service.inspect_thread(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            limit=limit,
        )
    finally:
        await service.aclose()


@chat.command(name="post")
@_person_option
@_service_option
@click.option("--channel-id", default="", help="Channel id of the target channel.")
@click.option(
    "--channel-name",
    default="",
    help="Channel name (alternative to --channel-id).",
)
@_required_content_stdin_option
@_json_format_option
def chat_post(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    output_format: str,
) -> None:
    body = _read_stdin("message body")
    _run(
        _chat_post(
            person,
            service_name,
            channel_id or None,
            channel_name or None,
            body,
        ),
        output_format=output_format,
    )


async def _chat_post(
    person: str,
    service_name: str,
    channel_id: str | None,
    channel_name: str | None,
    body: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        payload = await service.post(
            channel_id=channel_id, channel_name=channel_name, body=body
        )
        RunStore().append_evidence(current_run_id(), "chat_post", payload)
        return payload
    finally:
        await service.aclose()


@chat.command(name="reply")
@_person_option
@_service_option
@click.option("--channel-id", default="", help="Channel id of the target channel.")
@click.option(
    "--channel-name",
    default="",
    help="Channel name (alternative to --channel-id).",
)
@click.option("--thread-ts", default="", help="Thread timestamp (with --channel-id).")
@click.option(
    "--message-url",
    default="",
    help="Slack message URL (alternative to channel/timestamp options).",
)
@_required_content_stdin_option
@_json_format_option
def chat_reply(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    thread_ts: str,
    message_url: str,
    output_format: str,
) -> None:
    body = _read_stdin("message body")
    ref = _resolve_message_reference(
        channel_id=channel_id or None,
        thread_ts=thread_ts or None,
        message_ts=None,
        message_url=message_url or None,
    )
    resolved_thread_ts = ref["thread_ts"] or ref["message_ts"]
    if not resolved_thread_ts:
        raise click.ClickException("Either --thread-ts or --message-url is required.")
    _run(
        _chat_reply(
            person,
            service_name,
            ref["channel_id"],
            channel_name or None,
            resolved_thread_ts,
            body,
        ),
        output_format=output_format,
    )


async def _chat_reply(
    person: str,
    service_name: str,
    channel_id: str | None,
    channel_name: str | None,
    thread_ts: str,
    body: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        payload = await service.reply(
            channel_id=channel_id,
            channel_name=channel_name,
            thread_ts=thread_ts,
            body=body,
        )
        RunStore().append_evidence(current_run_id(), "chat_reply", payload)
        return payload
    finally:
        await service.aclose()


@chat.group(name="reaction")
def chat_reaction() -> None:
    """Chat reaction operations."""


@chat_reaction.command(name="add")
@_person_option
@_service_option
@click.option("--channel-id", default="", help="Channel id of the target channel.")
@click.option(
    "--channel-name",
    default="",
    help="Channel name (alternative to --channel-id).",
)
@click.option("--message-ts", default="", help="Message timestamp (with --channel-id).")
@click.option(
    "--message-url",
    default="",
    help="Slack message URL (alternative to channel/timestamp options).",
)
@click.option(
    "--reaction",
    required=True,
    type=click.Choice(["ack", "agree", "celebrate", "support"]),
    help="Semantic reaction to add.",
)
@_json_format_option
def chat_reaction_add(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    message_ts: str,
    message_url: str,
    reaction: str,
    output_format: str,
) -> None:
    ref = _resolve_message_reference(
        channel_id=channel_id or None,
        thread_ts=None,
        message_ts=message_ts or None,
        message_url=message_url or None,
    )
    if not ref["message_ts"]:
        raise click.ClickException("Either --message-ts or --message-url is required.")
    _run(
        _chat_reaction_add(
            person,
            service_name,
            ref["channel_id"],
            channel_name or None,
            ref["message_ts"],
            reaction,
        ),
        output_format=output_format,
    )


async def _chat_reaction_add(
    person: str,
    service_name: str,
    channel_id: str | None,
    channel_name: str | None,
    message_ts: str,
    reaction: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberChatCapabilityService(
        member_person,
        context.team,
        context.logger,
        context.get_chat_service(),
        service_name=service_name,
    )
    try:
        payload = await service.add_reaction(
            channel_id=channel_id,
            channel_name=channel_name,
            message_ts=message_ts,
            reaction=reaction,
        )
        RunStore().append_evidence(current_run_id(), "chat_reaction", payload)
        return payload
    finally:
        await service.aclose()


@chat.command(name="noop")
@_person_option
@click.option("--run-id", required=True, help="Workflow run id.")
@_service_option
@click.option("--channel-id", required=True, help="Channel id of the triggering event.")
@click.option(
    "--thread-ts", required=True, help="Thread timestamp of the triggering event."
)
@click.option("--event-id", required=True, help="Event id of the chat trigger.")
@_required_content_stdin_option
@_json_format_option
def chat_noop(
    person: str,
    run_id: str,
    service_name: str,
    channel_id: str,
    thread_ts: str,
    event_id: str,
    output_format: str,
) -> None:
    reason = _read_stdin("no-op reason")
    _resolve(person)
    payload = {
        "service": service_name,
        "channel_id": channel_id,
        "thread_ts": thread_ts,
        "event_id": event_id,
        "reason": reason,
        "noop": True,
    }
    RunStore().append_evidence(run_id, "chat_noop", payload)
    _emit(payload, output_format)


@chat.command(name="complete")
@_person_option
@click.option("--run-id", required=True, help="Workflow run id.")
@_service_option
@click.option("--channel-id", required=True, help="Channel id of the triggering event.")
@click.option(
    "--thread-ts", required=True, help="Thread timestamp of the triggering event."
)
@click.option("--event-id", required=True, help="Event id of the chat trigger.")
@click.option(
    "--status",
    required=True,
    type=click.Choice(["done", "asking", "blocked"]),
    help="Run outcome.",
)
@_required_content_stdin_option
@_json_format_option
def chat_complete(
    person: str,
    run_id: str,
    service_name: str,
    channel_id: str,
    thread_ts: str,
    event_id: str,
    status: str,
    output_format: str,
) -> None:
    summary = _read_stdin("run summary")
    _resolve(person)
    subject_id = f"{service_name}:{channel_id}:{thread_ts}:{event_id}"
    try:
        payload = (
            RunStore()
            .complete_run(
                run_id,
                status,
                summary,
                subject_type="chat",
                subject_id=subject_id,
                person_id=person,
            )
            .to_dict()
        )
    except TaskRunError as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    _emit(payload, output_format)


@member.group()
def git() -> None:
    """Prepare, commit, push, and publish member git workspaces."""


@git.command(name="prepare")
@_person_option
@click.option(
    "--issue-url", default="", help="Ticket issue URL to prepare a workspace for."
)
@click.option("--pr-url", default="", help="PR URL whose head branch to check out.")
@click.option("--repo", default="", help="Target repository as <owner>/<repo>.")
@click.option(
    "--branch", default="", help="Branch to create or check out (with --repo)."
)
@_json_format_option
def git_prepare(
    person: str,
    issue_url: str,
    pr_url: str,
    repo: str,
    branch: str,
    output_format: str,
) -> None:
    _validate_prepare_anchor(issue_url, pr_url, repo, branch)
    _run(
        _git_prepare(
            person, issue_url or None, pr_url or None, repo or None, branch or None
        ),
        output_format=output_format,
    )


def _validate_prepare_anchor(
    issue_url: str, pr_url: str, repo: str, branch: str
) -> None:
    if branch and not repo:
        raise click.UsageError("--branch requires --repo.")
    if not (issue_url or pr_url or repo):
        raise click.UsageError(
            "Provide --issue-url, --pr-url, or --repo with --branch."
        )
    if repo and (issue_url or pr_url):
        raise click.UsageError(
            "--repo cannot be combined with --issue-url or --pr-url."
        )
    if repo and not branch:
        raise click.UsageError("--repo requires --branch.")


async def _git_prepare(
    person: str,
    issue_url: str | None,
    pr_url: str | None,
    repo: str | None,
    branch: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        return await service.prepare(
            issue_url=issue_url, pr_url=pr_url, repo=repo, branch=branch
        )
    finally:
        await service.aclose()


@git.command(name="commit")
@_person_option
@click.option(
    "--repo-path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to the member repository workspace.",
)
@_required_content_stdin_option
@_workspace_mode_option
@_json_format_option
def git_commit(
    person: str,
    repo_path: Path,
    workspace_mode: str,
    output_format: str,
) -> None:
    """Commit already-staged changes with the member identity.

    Stage the files you want with plain git (e.g. ``git add``) first; this
    command commits only what is staged and applies the member name/email to
    that single commit without changing the repository's git config.
    """
    message = _read_stdin("commit message")
    _run(
        _git_commit(person, repo_path, message, workspace_mode),
        output_format=output_format,
    )


async def _git_commit(
    person: str,
    repo_path: Path,
    message: str,
    workspace_mode: str,
) -> dict[str, Any]:
    task_run_id = current_task_run_id()
    _reject_current_workspace_mode_in_task_run(workspace_mode, task_run_id)
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        result = await service.commit(
            repo_path=repo_path,
            message=message,
            workspace_mode=_workspace_mode(workspace_mode),
            cwd=Path.cwd(),
        )
        payload = result.to_dict()
        TaskRunStore().append_evidence(task_run_id, "git_commit", payload)
        return payload
    finally:
        await service.aclose()


@git.command(name="push")
@_person_option
@click.option(
    "--repo-path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to the member repository workspace.",
)
@_workspace_mode_option
@_json_format_option
def git_push(
    person: str,
    repo_path: Path,
    workspace_mode: str,
    output_format: str,
) -> None:
    _run(
        _git_push(person, repo_path, workspace_mode),
        output_format=output_format,
    )


async def _git_push(
    person: str,
    repo_path: Path,
    workspace_mode: str,
) -> dict[str, Any]:
    task_run_id = current_task_run_id()
    _reject_current_workspace_mode_in_task_run(workspace_mode, task_run_id)
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        result = await service.push(
            repo_path=repo_path,
            workspace_mode=_workspace_mode(workspace_mode),
            cwd=Path.cwd(),
        )
        payload = result.to_dict()
        TaskRunStore().append_evidence(task_run_id, "git_push", payload)
        record_member_push_event(member_person, payload)
        return payload
    finally:
        await service.aclose()


@git.command(name="publish")
@_person_option
@click.option(
    "--repo-path",
    required=True,
    type=click.Path(path_type=Path),
    help="Path to the member repository workspace.",
)
@_required_content_stdin_option
@_workspace_mode_option
@_json_format_option
def git_publish(
    person: str,
    repo_path: Path,
    workspace_mode: str,
    output_format: str,
) -> None:
    """Commit already-staged changes with the member identity, then push.

    Stage the files you want with plain git (e.g. ``git add``) first; this
    commits only what is staged with the member name/email and pushes the
    branch using the member credential.
    """
    message = _read_stdin("commit message")
    result = _run(
        _git_publish(person, repo_path, message, workspace_mode),
        output_format=output_format,
    )
    return result


async def _git_publish(
    person: str,
    repo_path: Path,
    message: str,
    workspace_mode: str,
) -> dict[str, Any]:
    task_run_id = current_task_run_id()
    _reject_current_workspace_mode_in_task_run(workspace_mode, task_run_id)
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        if workspace_mode == "current":
            result = await service.publish_current_workspace(
                repo_path=repo_path, message=message, cwd=Path.cwd()
            )
        else:
            result = await service.publish(repo_path=repo_path, message=message)
        payload = result.to_dict()
        TaskRunStore().append_evidence(task_run_id, "git_publish", payload)
        record_member_push_event(member_person, payload)
        return payload
    finally:
        await service.aclose()


def _reject_current_workspace_mode_in_task_run(
    workspace_mode: str, task_run_id: str | None
) -> None:
    if workspace_mode == "current" and task_run_id:
        raise click.ClickException(
            "workspace-mode=current is only for interactive use and cannot be "
            "used inside a workflow task run."
        )


def _workspace_mode(value: str) -> WorkspaceMode:
    if value not in {"member", "current"}:
        raise click.ClickException(f"Unsupported workspace mode: {value}")
    return cast(WorkspaceMode, value)


@member.group()
def github() -> None:
    """GitHub issue, pull request, and reaction capabilities."""


@github.group()
def issue() -> None:
    """GitHub issue operations."""


@issue.command(name="inspect")
@_read_only_member_command
@_person_option
@click.option("--url", "issue_url", required=True, help="Issue URL.")
@_markdown_format_option
def issue_inspect(person: str, issue_url: str, output_format: str) -> None:
    _run(_issue_inspect(person, issue_url), output_format=output_format)


async def _issue_inspect(person: str, issue_url: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        return await service.issue_inspect(issue_url)
    finally:
        await service.aclose()


@issue.command(name="comment")
@_person_option
@click.option("--url", "issue_url", required=True, help="Issue URL.")
@_required_content_stdin_option
@_json_format_option
def issue_comment(
    person: str,
    issue_url: str,
    output_format: str,
) -> None:
    body = _read_stdin("issue comment body")
    _run(
        _issue_comment(person, issue_url, body),
        output_format=output_format,
    )


async def _issue_comment(person: str, issue_url: str, body: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.issue_comment(issue_url, body)
        TaskRunStore().append_evidence(current_task_run_id(), "issue_comment", result)
        record_member_issue_comment_event(member_person, result)
        return result
    finally:
        await service.aclose()


@issue.command(name="create")
@_person_option
@click.option("--repo", required=True, help="Target repository as <owner>/<repo>.")
@click.option("--title", required=True, help="Issue title.")
@_required_content_stdin_option
@click.option(
    "--add-to-project/--no-add-to-project",
    default=True,
    help="Add the created issue to the configured project board.",
)
@_json_format_option
def issue_create(
    person: str,
    repo: str,
    title: str,
    add_to_project: bool,
    output_format: str,
) -> None:
    title = _validate_title(title)
    body = _read_stdin("issue body")
    _run(
        _issue_create(person, repo, title, body, add_to_project),
        output_format=output_format,
    )


async def _issue_create(
    person: str,
    repo: str,
    title: str,
    body: str,
    add_to_project: bool,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.issue_create(repo, title, body, add_to_project)
        TaskRunStore().append_evidence(current_task_run_id(), "issue_create", result)
        record_member_issue_create_event(member_person, result)
        return result
    finally:
        await service.aclose()


@issue.command(name="update")
@_person_option
@click.option("--url", "issue_url", required=True, help="Issue URL.")
@_required_content_stdin_option
@_json_format_option
def issue_update(
    person: str,
    issue_url: str,
    output_format: str,
) -> None:
    body = _read_stdin("issue body", allow_empty=True)
    _run(
        _issue_update(person, issue_url, body),
        output_format=output_format,
    )


async def _issue_update(person: str, issue_url: str, body: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.issue_update(issue_url, body)
        TaskRunStore().append_evidence(current_task_run_id(), "issue_update", result)
        return result
    finally:
        await service.aclose()


@github.group()
def pr() -> None:
    """GitHub pull request operations."""


@pr.command(name="inspect")
@_read_only_member_command
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@click.option(
    "--include-comments",
    is_flag=True,
    help="Include review threads with their reply target ids.",
)
@click.option(
    "--include-diff",
    is_flag=True,
    help="Include the diff with commentable line coordinates.",
)
@_markdown_format_option
def pr_inspect(
    person: str,
    pr_url: str,
    include_comments: bool,
    include_diff: bool,
    output_format: str,
) -> None:
    _run(
        _pr_inspect(person, pr_url, include_comments, include_diff),
        output_format=output_format,
    )


async def _pr_inspect(
    person: str, pr_url: str, include_comments: bool, include_diff: bool
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        return await service.pr_inspect(pr_url, include_comments, include_diff)
    finally:
        await service.aclose()


@pr.command(name="create")
@_person_option
@click.option("--repo", required=True, help="Target repository as <owner>/<repo>.")
@click.option("--head", required=True, help="Head branch containing the changes.")
@click.option(
    "--base",
    default="",
    help="Base branch for the pull request. Defaults to the repository default branch.",
)
@click.option(
    "--title",
    required=True,
    help="Pull request title.",
)
@_required_content_stdin_option
@click.option("--issue-url", default="", help="Related issue URL to link to the PR.")
@click.option(
    "--draft",
    type=click.Choice(["auto", "true", "false"]),
    default="auto",
    help="Open as a draft PR; 'auto' drafts when the member is a proxy agent.",
)
@_json_format_option
def pr_create(
    person: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    issue_url: str,
    draft: str,
    output_format: str,
) -> None:
    title = _validate_title(title)
    body = _read_stdin("pull request body")
    _run(
        _pr_create(person, repo, head, base, title, body, issue_url, draft),
        output_format=output_format,
    )


async def _pr_create(
    person: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    issue_url: str,
    draft: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_create(
            repo, head, base, title, body, issue_url, draft
        )
        TaskRunStore().append_evidence(current_task_run_id(), "pr_create", result)
        record_member_pr_create_event(member_person, repo, title, result)
        return result
    finally:
        await service.aclose()


@pr.command(name="update")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@_required_content_stdin_option
@_json_format_option
def pr_update(
    person: str,
    pr_url: str,
    output_format: str,
) -> None:
    body = _read_stdin("pull request body", allow_empty=True)
    _run(
        _pr_update(person, pr_url, body),
        output_format=output_format,
    )


async def _pr_update(person: str, pr_url: str, body: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_update(pr_url, body)
        TaskRunStore().append_evidence(current_task_run_id(), "pr_update", result)
        return result
    finally:
        await service.aclose()


@pr.command(name="comment")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@_required_content_stdin_option
@_json_format_option
def pr_comment(person: str, pr_url: str, output_format: str) -> None:
    body = _read_stdin("pull request comment body")
    _run(
        _pr_comment(person, pr_url, body),
        output_format=output_format,
    )


async def _pr_comment(person: str, pr_url: str, body: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_comment(pr_url, body)
        TaskRunStore().append_evidence(current_task_run_id(), "pr_comment", result)
        return result
    finally:
        await service.aclose()


@pr.command(name="review-comment")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@click.option("--path", "file_path", required=True, help="File path in the PR diff.")
@click.option(
    "--line",
    required=True,
    type=click.IntRange(min=1),
    help="Line number on the chosen diff side.",
)
@click.option(
    "--side",
    type=click.Choice(["LEFT", "RIGHT"]),
    default="RIGHT",
    help="Diff side of the line.",
)
@click.option(
    "--start-line",
    type=click.IntRange(min=1),
    default=None,
    help="Start line for a multi-line comment.",
)
@click.option(
    "--start-side",
    type=click.Choice(["LEFT", "RIGHT"]),
    default=None,
    help="Diff side of --start-line.",
)
@_required_content_stdin_option
@_json_format_option
def pr_review_comment(
    person: str,
    pr_url: str,
    file_path: str,
    line: int,
    side: str,
    start_line: int | None,
    start_side: str | None,
    output_format: str,
) -> None:
    if (start_line is None) != (start_side is None):
        raise click.ClickException(
            "--start-line and --start-side must be provided together."
        )
    body = _read_stdin("pull request review comment body")
    _run(
        _pr_review_comment(
            person, pr_url, body, file_path, line, side, start_line, start_side
        ),
        output_format=output_format,
    )


async def _pr_review_comment(
    person: str,
    pr_url: str,
    body: str,
    file_path: str,
    line: int,
    side: str,
    start_line: int | None,
    start_side: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_review_comment(
            pr_url, body, file_path, line, side, start_line, start_side
        )
        TaskRunStore().append_evidence(
            current_task_run_id(), "pr_review_comment", result
        )
        return result
    finally:
        await service.aclose()


@pr.command(name="reply")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@click.option(
    "--reply-target-id",
    required=True,
    type=int,
    help="reply_target_id from 'pr inspect --include-comments'.",
)
@_required_content_stdin_option
@_json_format_option
def pr_reply(
    person: str,
    pr_url: str,
    reply_target_id: int,
    output_format: str,
) -> None:
    body = _read_stdin("pull request reply body")
    _run(
        _pr_reply(person, pr_url, reply_target_id, body),
        output_format=output_format,
    )


async def _pr_reply(
    person: str,
    pr_url: str,
    reply_target_id: int,
    body: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_reply(pr_url, reply_target_id, body)
        TaskRunStore().append_evidence(current_task_run_id(), "pr_reply", result)
        return result
    finally:
        await service.aclose()


@github.group()
def reaction() -> None:
    """GitHub reaction operations."""


@reaction.command(name="add")
@_person_option
@click.option("--repo", required=True, help="Target repository as <owner>/<repo>.")
@click.option(
    "--target",
    required=True,
    type=click.Choice(["issue-comment", "pr-review-comment"]),
    help="Kind of comment to react to.",
)
@click.option(
    "--comment-id", required=True, type=int, help="Numeric id of the comment."
)
@click.option(
    "--reaction",
    "reaction_content",
    required=True,
    type=click.Choice(
        ["+1", "eyes", "heart", "hooray", "rocket", "laugh", "confused", "-1"]
    ),
    help="Reaction to add.",
)
@_json_format_option
def reaction_add(
    person: str,
    repo: str,
    target: str,
    comment_id: int,
    reaction_content: str,
    output_format: str,
) -> None:
    _run(
        _reaction_add(person, repo, target, comment_id, reaction_content),
        output_format=output_format,
    )


async def _reaction_add(
    person: str,
    repo: str,
    target: str,
    comment_id: int,
    reaction_content: str,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.reaction_add(repo, target, comment_id, reaction_content)
        TaskRunStore().append_evidence(current_task_run_id(), "reaction_add", result)
        return result
    finally:
        await service.aclose()


@member.group()
def task() -> None:
    """Workflow task-run completion records."""


@task.command(name="complete")
@_person_option
@click.option("--run-id", required=True, help="Workflow run id.")
@click.option(
    "--ticket-url", required=True, help="Ticket URL the completed run worked on."
)
@click.option(
    "--status",
    required=True,
    type=click.Choice(["done", "asking", "blocked"]),
    help="Run outcome.",
)
@_required_content_stdin_option
@_json_format_option
def task_complete(
    person: str,
    run_id: str,
    ticket_url: str,
    status: str,
    output_format: str,
) -> None:
    summary = _read_stdin("run summary")
    _run(
        _task_complete(person, run_id, ticket_url, status, summary),
        output_format=output_format,
    )


async def _task_complete(
    person: str, run_id: str, ticket_url: str, status: str, summary: str
) -> dict[str, Any]:
    _resolve(person)
    store = TaskRunStore()
    try:
        return store.complete(run_id, status, summary, ticket_url, person).to_dict()
    except TaskRunError as exc:
        raise click.ClickException(_safe_error(exc)) from exc


@task.command(name="status")
@_read_only_member_command
@click.option("--run-id", required=True, help="Workflow run id.")
@click.option(
    "--person",
    default="",
    help="Accepted for consistency with other member commands; not required.",
)
@_json_format_option
def task_status(run_id: str, person: str, output_format: str) -> None:
    _run(_task_status(run_id, person), output_format=output_format)


async def _task_status(run_id: str, person: str) -> dict[str, Any]:
    del person
    try:
        return TaskRunStore().status(run_id).to_dict()
    except TaskRunError as exc:
        raise click.ClickException(_safe_error(exc)) from exc


def _resolve(person: str):
    try:
        return resolve_member_context(person)
    except PersonNotFoundError as exc:
        message = f"Unknown member '{exc.identifier}'."
        if exc.available:
            message = f"{message} Available members: {', '.join(exc.available)}."
        raise click.ClickException(message) from exc


def _read_stdin(label: str, *, allow_empty: bool = False) -> str:
    text = click.get_text_stream("stdin").read()
    if text.strip():
        return text
    if allow_empty:
        return ""
    raise click.ClickException(f"{label} must not be empty.")


def _read_optional_stdin(content_stdin: bool, label: str) -> str | None:
    if not content_stdin:
        return None
    return _read_stdin(label)


def _validate_title(title: str) -> str:
    if "\n" in title or "\r" in title:
        raise click.ClickException("title must not contain newlines.")
    normalized = title.strip()
    if not normalized:
        raise click.ClickException("title must not be empty.")
    return normalized


def _source_entries(
    tickets: tuple[str, ...],
    prs: tuple[str, ...],
    channels: tuple[str, ...],
    threads: tuple[str, ...],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for source_type, values in (
        ("ticket", tickets),
        ("pr", prs),
        ("channel", channels),
        ("thread", threads),
    ):
        for value in values:
            if value.strip():
                entries.append({"type": source_type, "url": value.strip()})
    return entries


def _parse_set_values(values: tuple[str, ...]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in values:
        key, separator, value = raw.partition("=")
        key = key.strip()
        if not key or not separator:
            raise click.ClickException("--set values must use key=value syntax.")
        parsed[key] = _parse_scalar(value.strip())
    return parsed


def _parse_scalar(value: str) -> Any:
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _resolve_message_reference(
    *,
    channel_id: str | None,
    thread_ts: str | None,
    message_ts: str | None,
    message_url: str | None,
) -> dict[str, str | None]:
    if not message_url:
        return {
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "message_ts": message_ts,
        }

    parsed = _parse_slack_message_url(message_url)
    return {
        "channel_id": channel_id or parsed["channel_id"],
        "thread_ts": thread_ts or parsed["thread_ts"],
        "message_ts": message_ts or parsed["message_ts"],
    }


def _parse_slack_message_url(message_url: str) -> dict[str, str | None]:
    parsed = urlparse(message_url)
    parts = [part for part in parsed.path.split("/") if part]
    try:
        archives_index = parts.index("archives")
        channel_id = parts[archives_index + 1]
        raw_message_id = parts[archives_index + 2]
    except (ValueError, IndexError) as exc:
        raise click.ClickException(
            "Slack message URL must be an /archives/... URL."
        ) from exc

    message_ts = _slack_permalink_ts(raw_message_id)
    query = parse_qs(parsed.query)
    thread_values = query.get("thread_ts", [])
    thread_ts = thread_values[0] if thread_values else message_ts
    return {
        "channel_id": channel_id,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
    }


def _slack_permalink_ts(raw_message_id: str) -> str:
    if not raw_message_id.startswith("p"):
        raise click.ClickException("Slack message URL does not contain a message id.")
    digits = raw_message_id[1:]
    if not digits.isdigit() or len(digits) <= SLACK_TS_FRACTION_DIGITS:
        raise click.ClickException("Slack message URL contains an invalid message id.")
    return f"{digits[:-SLACK_TS_FRACTION_DIGITS]}.{digits[-SLACK_TS_FRACTION_DIGITS:]}"


def _run(coro, *, output_format: str) -> Any:
    interactive_session = _interactive_session_for_current_command()
    command = _current_command_path()
    started = False
    try:
        with _member_execution_guard(command, interactive_session):
            started = True
            if interactive_session is None:
                result = asyncio.run(coro)
            else:
                result = _run_interactive(coro, interactive_session, command)
    except (MemberCapabilityError, MemberMemoryError, TaskRunError, KeyError) as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    except BaseException:
        if not started and asyncio.iscoroutine(coro):
            coro.close()
        raise
    _emit(result, output_format)
    return result


@contextmanager
def _member_execution_guard(command: str, session: InteractiveTraceSession | None):
    if not _member_command_needs_lease():
        yield
        return
    person = _current_person()
    if not person:
        yield
        return
    from guildbotics.runtime.person_lease import (
        LEASE_PERSON_ENV,
        PersonExecutionLease,
        PersonLeaseUnavailableError,
        validate_delegation,
    )

    if _running_under_workflow():
        delegated_person = os.getenv(LEASE_PERSON_ENV, "")
        person_id = delegated_person if delegated_person == person else ""
        if not person_id:
            _context, member_person = _resolve(person)
            person_id = member_person.person_id
        if validate_delegation(person_id) is None:
            raise click.ClickException(t("cli.member.lease.invalid_delegation"))
        yield
        return
    _context, member_person = _resolve(person)
    lease = PersonExecutionLease(member_person.person_id)
    try:
        lease.acquire(
            source="interactive",
            command=command,
            work_id=session.trace_id if session is not None else uuid4().hex,
        )
    except PersonLeaseUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        yield
    finally:
        lease.release()


def _member_command_needs_lease() -> bool:
    context = click.get_current_context(silent=True)
    callback = context.command.callback if context is not None else None
    # New or malformed commands fail closed as write-capable until their callback
    # explicitly declares that it is read-only.
    return not bool(getattr(callback, _READ_ONLY_COMMAND_ATTRIBUTE, False))


def _run_interactive(
    coro, session: InteractiveTraceSession, command: str
) -> dict[str, Any]:
    store = InteractiveTraceStore()
    try:
        with trace_scope(
            "interactive",
            person_id=session.person_id,
            command=command,
            attributes=session.attributes,
            trace_id=session.trace_id,
        ):
            _record_member_command_event("member.command.started", command)
            try:
                result = asyncio.run(coro)
            except Exception as exc:
                _record_member_command_event(
                    "member.command.failed",
                    command,
                    {"error_type": type(exc).__name__},
                )
                raise
            _record_member_command_event("member.command.finished", command)
            return cast(dict[str, Any], result)
    finally:
        store.touch(session)


def _interactive_session_for_current_command() -> InteractiveTraceSession | None:
    if _running_under_workflow():
        return None
    person = _current_person()
    if not person:
        return None
    try:
        _context, member_person = _resolve(person)
    except (click.ClickException, FileNotFoundError):
        return None
    if member_person.person_type == "human":
        return None
    workspace = _current_workspace()
    return InteractiveTraceStore().start_or_touch(
        person_id=member_person.person_id,
        workspace=workspace,
        host=interactive_host(),
        thread_key=interactive_thread_key(),
    )


def _running_under_workflow() -> bool:
    return bool(os.getenv(TASK_RUN_ENV) or os.getenv(RUN_ENV))


def _current_person() -> str:
    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        value = ctx.params.get("person") if ctx.params else None
        if isinstance(value, str) and value:
            return value
        ctx = ctx.parent
    return ""


def _current_workspace() -> str:
    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        workspace = ctx.obj.get("workspace") if isinstance(ctx.obj, dict) else None
        if isinstance(workspace, str) and workspace:
            return workspace
        ctx = ctx.parent
    return str(Path.cwd().resolve())


def _current_command_path() -> str:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return "member"
    return ctx.command_path


def _record_member_command_event(
    event_type: str, command: str, payload: dict[str, Any] | None = None
) -> None:
    record_correlated_event(
        event_type=event_type,
        command=command,
        payload={"command": command, **(payload or {})},
    )


def _emit(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    click.echo(_to_markdown(payload))


def _to_markdown(payload: dict[str, Any]) -> str:
    lines = []
    for key, value in payload.items():
        if key == "communication_style" and isinstance(value, dict):
            lines.append("## Communication Style")
            for style_key in (
                "active_member_instruction",
                "voice_basis",
                "interactive_replies",
                "github_comments",
                "neutral_documents",
                "machine_outputs",
            ):
                if style_key in value:
                    lines.append(f"- **{style_key}**: {value[style_key]}")
            continue
        if key == "capabilities" and isinstance(value, str):
            lines.append("## Member Capabilities")
            lines.append(value)
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        lines.append(f"- **{key}**: {rendered}")
    return "\n".join(lines)


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for marker in ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY"):
        if marker in text.upper():
            return "Member credential could not be resolved or used safely."
    return text or "Member capability command failed."


def _fill_help_from_catalog(group: click.Group, path: tuple[str, ...] = ()) -> None:
    """Fill missing command help from the member capability catalog.

    The catalog in ``member_reference`` is the single source of the one-line
    command purposes; commands without their own docstring get theirs from it
    (and a command absent from the catalog fails fast here).
    """
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            _fill_help_from_catalog(command, (*path, name))
        elif command.help is None:
            command.help = command_summary(" ".join((*path, name)))


_fill_help_from_catalog(member)
