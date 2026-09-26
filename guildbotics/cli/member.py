from __future__ import annotations

import asyncio
import io
import json
import re
import traceback
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Literal, TextIO, cast
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import click
from pydantic import ValidationError

from guildbotics.capabilities.chat_updates import (
    ChatUpdatesRequired,
    check_chat_updates,
)
from guildbotics.capabilities.command_failures import command_failure_payload
from guildbotics.capabilities.member_activity_events import (
    record_member_issue_close_event,
    record_member_issue_comment_event,
    record_member_issue_create_event,
    record_member_pr_create_event,
    record_member_push_event,
    record_member_work_target,
)
from guildbotics.capabilities.member_chat import MemberChatCapabilityService
from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    DEFAULT_LOG_TAIL_BYTES,
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
    RunStore,
    TaskRunError,
    TaskRunStore,
    current_run_id,
    current_task_run_id,
)
from guildbotics.cli._options import (
    CLI_CONTEXT_SETTINGS,
    SharedWriteBusyGroup,
    apply_workspace_option,
    format_option,
    workspace_option,
)
from guildbotics.commands.errors import (
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
)
from guildbotics.observability import join_trace, trace_scope
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.observability.interactive_sessions import (
    InteractiveSessionStore,
    InteractiveTraceSession,
    InteractiveTraceStore,
    interactive_host,
    interactive_thread_key,
)
from guildbotics.runtime.member_context import resolve_member_context
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    current_member_invocation,
    member_invocation_scope,
)
from guildbotics.runtime.person_lease import (
    PersonExecutionLease,
    PersonLeaseUnavailableError,
)
from guildbotics.sync.activation import (
    ONE_SHOT_LOCK_TIMEOUT_SECONDS,
    PreparedOneShotSync,
    prepare_commit_and_push_once,
)
from guildbotics.utils.diagnostics_records import diagnostics_record_scope
from guildbotics.utils.fileio import get_workspace_root
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.shared_write_lock import SharedWriteBusyError
from guildbotics.utils.sync_lock import SyncRepositoryBusyError
from guildbotics.workspace.identity import (
    DeviceIdentity,
    WorkspaceIdentity,
    device_identity_path,
    workspace_identity_path,
)

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
_CONTENT_META_KEY = "guildbotics.member.content"


def _content_option(
    *, required: bool
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(callback: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(callback)
        def wrapped(
            *args: Any,
            content_stdin: bool,
            content_file: Path | None,
            **kwargs: Any,
        ) -> Any:
            if content_stdin and content_file is not None:
                raise click.UsageError(t("cli.member.content.exclusive"))
            selected = content_stdin or content_file is not None
            if required and not selected:
                raise click.UsageError(t("cli.member.content.required"))

            context = click.get_current_context()
            if selected:
                context.meta[_CONTENT_META_KEY] = _read_content_source(content_file)
            if not required:
                kwargs["content_stdin"] = selected
            try:
                return callback(*args, **kwargs)
            finally:
                context.meta.pop(_CONTENT_META_KEY, None)

        with_file = click.option(
            "--content-file",
            type=_HostPath(exists=True, dir_okay=False, readable=True),
            help=t("cli.member.content.file_help"),
        )(wrapped)
        return click.option(
            "--content-stdin",
            is_flag=True,
            help=t("cli.member.content.stdin_help"),
        )(with_file)

    return decorate


_required_content_stdin_option = _content_option(required=True)
_optional_content_stdin_option = _content_option(required=False)
_human_approved_option = click.option(
    "--human-approved",
    is_flag=True,
    help="Confirm that a human instructed or approved this change.",
)


@dataclass(frozen=True, slots=True)
class MemberCall:
    """Where one member command resolves paths, reads input, and prints.

    The defaults are the process's own. A command run inside the host process
    that owns the member broker gets its own instead, because that process's
    working directory and standard streams are shared by every command running
    beside it.
    """

    cwd: Path = field(default_factory=Path.cwd)
    stdin: str | None = None
    stdout: TextIO | None = None


def _call(ctx: click.Context | None = None) -> MemberCall:
    ctx = ctx or click.get_current_context(silent=True)
    found = ctx.find_object(MemberCall) if ctx is not None else None
    return found or MemberCall()


class _HostPath(click.Path):
    """A path read from the command's working directory, then checked as usual."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(path_type=Path, **kwargs)

    def convert(
        self, value: Any, param: click.Parameter | None, ctx: click.Context | None
    ) -> Any:
        return super().convert(_call(ctx).cwd / value, param, ctx)


def _show_help(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if value and not ctx.resilient_parsing:
        click.echo(ctx.get_help(), color=ctx.color, file=_call(ctx).stdout)
        ctx.exit()


def _help_to_call(option: click.Option | None) -> click.Option | None:
    """Print ``--help`` where the command's :class:`MemberCall` prints."""
    if option is not None:
        option.callback = _show_help
    return option


class _MemberCommand(click.Command):
    def get_help_option(self, ctx: click.Context) -> click.Option | None:
        return _help_to_call(super().get_help_option(ctx))


class _MemberGroup(SharedWriteBusyGroup):
    command_class = _MemberCommand
    group_class = type

    def get_help_option(self, ctx: click.Context) -> click.Option | None:
        return _help_to_call(super().get_help_option(ctx))


@click.group(cls=_MemberGroup, context_settings=CLI_CONTEXT_SETTINGS)
@click.pass_context
@workspace_option
def member(ctx: click.Context, workspace_dir: Path | None) -> None:
    """Operate as a configured GuildBotics member."""
    # A command run inside the host process uses the workspace it already has.
    if ctx.find_object(MemberCall) is None:
        apply_workspace_option(workspace_dir)


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
    click.echo(capability_reference_text(), file=_call().stdout)


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
    type=click.Choice(["codex", "claude", "grok"]),
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
    store = ConversationStore(get_workspace_root())
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
@_optional_content_stdin_option
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


@chat.command(name="updates")
@_person_option
@click.option(
    "--run-id",
    required=True,
    help="Chat workflow run whose source thread should be checked.",
)
@_json_format_option
def chat_updates(person: str, run_id: str, output_format: str) -> None:
    _run(_chat_updates(person, run_id), output_format=output_format)


async def _chat_updates(person: str, run_id: str) -> dict[str, Any]:
    context, member_person = _resolve(person)
    try:
        return check_chat_updates(member_person.person_id, run_id)
    finally:
        await context.aclose()


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
    type=_HostPath(),
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
            cwd=_call().cwd,
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
    type=_HostPath(),
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
            cwd=_call().cwd,
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
    type=_HostPath(),
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
                repo_path=repo_path, message=message, cwd=_call().cwd
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
    """GitHub issue, pull request, Actions, and reaction capabilities."""


async def _github(
    person: str,
    action: Callable[[MemberGitHubCapabilityService], Awaitable[dict[str, Any]]],
    *,
    evidence: str = "",
    then: Callable[[Any, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one GitHub capability and record the PR / issue it worked on.

    Every issue and pull request command declares its target here, in one
    place, so the trace the command runs inside is titled by whichever item
    a command touched (or read) first.
    """
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await action(service)
    finally:
        await service.aclose()
    if evidence:
        TaskRunStore().append_evidence(current_task_run_id(), evidence, result)
    target = result.get("target")
    if isinstance(target, dict):
        record_member_work_target(
            member_person, target, read_only=not _member_command_needs_lease()
        )
    if then is not None:
        then(member_person, result)
    return result


@github.group()
def issue() -> None:
    """GitHub issue operations."""


@issue.command(name="inspect")
@_read_only_member_command
@_person_option
@click.option("--url", "issue_url", required=True, help="Issue URL.")
@_markdown_format_option
def issue_inspect(person: str, issue_url: str, output_format: str) -> None:
    _run(
        _github(person, lambda service: service.issue_inspect(issue_url)),
        output_format=output_format,
    )


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
        _github(
            person,
            lambda service: service.issue_comment(issue_url, body),
            evidence="issue_comment",
            then=record_member_issue_comment_event,
        ),
        output_format=output_format,
    )


@issue.command(name="create")
@_person_option
@click.option("--repo", required=True, help="Target repository as <owner>/<repo>.")
@click.option("--title", required=True, help="Issue title.")
@_required_content_stdin_option
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Label already defined in the repository. Repeat for several labels.",
)
@click.option(
    "--add-to-project/--no-add-to-project",
    default=True,
    help="Add the created issue to the configured project board.",
)
@_human_approved_option
@_json_format_option
def issue_create(
    person: str,
    repo: str,
    title: str,
    labels: tuple[str, ...],
    add_to_project: bool,
    human_approved: bool,
    output_format: str,
) -> None:
    title = _validate_title(title)
    body = _read_stdin("issue body")
    _run(
        _github(
            person,
            lambda service: service.issue_create(
                repo, title, body, add_to_project, list(labels), human_approved
            ),
            evidence="issue_create",
            then=record_member_issue_create_event,
        ),
        output_format=output_format,
    )


@issue.command(name="update")
@_person_option
@click.option("--url", "issue_url", required=True, help="Issue URL.")
@_optional_content_stdin_option
@click.option("--title", default=None, help="Replace the issue title.")
@click.option(
    "--add-label",
    "add_labels",
    multiple=True,
    help="Add a label already defined in the repository. Repeat for several labels.",
)
@click.option(
    "--remove-label",
    "remove_labels",
    multiple=True,
    help="Remove a label from the issue. Repeat for several labels.",
)
@click.option(
    "--state",
    type=click.Choice(["open", "closed"]),
    help="Close or reopen the issue; requires --human-approved.",
)
@click.option(
    "--state-reason",
    type=click.Choice(["completed", "not_planned"]),
    help="Why the issue is closed; requires --state closed.",
)
@_human_approved_option
@_json_format_option
def issue_update(
    person: str,
    issue_url: str,
    content_stdin: bool,
    title: str | None,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
    state: str | None,
    state_reason: str | None,
    human_approved: bool,
    output_format: str,
) -> None:
    if state_reason and state != "closed":
        raise click.UsageError("--state-reason requires --state closed.")
    if not (content_stdin or title is not None or add_labels or remove_labels or state):
        raise click.UsageError(
            "issue update needs --content-stdin/--content-file, --title, --add-label, "
            "--remove-label, or --state."
        )
    body = _read_stdin("issue body", allow_empty=True) if content_stdin else None
    new_title = _validate_title(title) if title is not None else None
    _run(
        _github(
            person,
            lambda service: service.issue_update(
                issue_url,
                body=body,
                title=new_title,
                add_labels=list(add_labels),
                remove_labels=list(remove_labels),
                state=state,
                state_reason=state_reason,
                human_approved=human_approved,
            ),
            evidence="issue_update",
            then=record_member_issue_close_event,
        ),
        output_format=output_format,
    )


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
    help=(
        "Include conversation comments, review summaries, and review threads "
        "with their reply target ids."
    ),
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
        _github(
            person,
            lambda service: service.pr_inspect(pr_url, include_comments, include_diff),
        ),
        output_format=output_format,
    )


@pr.command(name="checks")
@_read_only_member_command
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@click.option(
    "--failed-logs",
    is_flag=True,
    help="Include bounded log tails for failed GitHub Actions jobs.",
)
@click.option(
    "--log-tail-bytes",
    type=click.IntRange(min=1),
    default=DEFAULT_LOG_TAIL_BYTES,
    show_default=True,
    help="Maximum bytes returned from the end of each failed job log.",
)
@_markdown_format_option
def pr_checks(
    person: str,
    pr_url: str,
    failed_logs: bool,
    log_tail_bytes: int,
    output_format: str,
) -> None:
    _run(
        _github(
            person,
            lambda service: service.pr_checks(
                pr_url, failed_logs=failed_logs, log_tail_bytes=log_tail_bytes
            ),
        ),
        output_format=output_format,
    )


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
    "--closes-issue/--refs-issue",
    "closes_issue",
    default=False,
    help=(
        "When --issue-url is set, append Closes #<n> or Refs #<n> to the PR "
        "body. Defaults to Refs."
    ),
)
@click.option(
    "--draft",
    type=click.Choice(["true", "false"]),
    default="false",
    help="Open the pull request as a draft.",
)
@_json_format_option
def pr_create(
    person: str,
    repo: str,
    head: str,
    base: str,
    title: str,
    issue_url: str,
    closes_issue: bool,
    draft: str,
    output_format: str,
) -> None:
    title = _validate_title(title)
    if closes_issue and not issue_url.strip():
        raise click.UsageError("--closes-issue requires --issue-url.")
    body = _read_stdin("pull request body")
    _run(
        _github(
            person,
            lambda service: service.pr_create(
                repo, head, base, title, body, issue_url, draft, closes_issue
            ),
            evidence="pr_create",
            then=lambda member, result: record_member_pr_create_event(
                member, repo, title, result
            ),
        ),
        output_format=output_format,
    )


@pr.command(name="update")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@_optional_content_stdin_option
@click.option("--title", default=None, help="Replace the pull request title.")
@click.option(
    "--drop-issue-links",
    is_flag=True,
    help=(
        "Do not carry existing Closes/Fixes/Resolves/Refs issue links into the "
        "replacement body. Requires --content-stdin/--content-file. By default, "
        "links are preserved, even with empty content; new links to the same "
        "issue take precedence."
    ),
)
@_json_format_option
def pr_update(
    person: str,
    pr_url: str,
    content_stdin: bool,
    title: str | None,
    drop_issue_links: bool,
    output_format: str,
) -> None:
    if drop_issue_links and not content_stdin:
        raise click.UsageError(
            "--drop-issue-links requires --content-stdin/--content-file."
        )
    if not (content_stdin or title is not None):
        raise click.UsageError(
            "pr update needs --content-stdin/--content-file or --title."
        )
    body = _read_stdin("pull request body", allow_empty=True) if content_stdin else None
    new_title = _validate_title(title) if title is not None else None
    _run(
        _github(
            person,
            lambda service: service.pr_update(
                pr_url, body=body, title=new_title, drop_issue_links=drop_issue_links
            ),
            evidence="pr_update",
        ),
        output_format=output_format,
    )


@pr.command(name="comment")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@_required_content_stdin_option
@_json_format_option
def pr_comment(person: str, pr_url: str, output_format: str) -> None:
    body = _read_stdin("pull request comment body")
    _run(
        _github(
            person,
            lambda service: service.pr_comment(pr_url, body),
            evidence="pr_comment",
        ),
        output_format=output_format,
    )


@pr.command(name="review")
@_person_option
@click.option("--url", "pr_url", required=True, help="Pull request URL.")
@click.option(
    "--event",
    required=True,
    type=click.Choice(["approve", "request-changes", "comment"]),
    help="Review verdict submitted on the current PR head.",
)
@_required_content_stdin_option
@_json_format_option
def pr_review(person: str, pr_url: str, event: str, output_format: str) -> None:
    body = _read_stdin("pull request review body")
    _run(
        _github(
            person,
            lambda service: service.pr_review(pr_url, body, event),
            evidence="pr_review",
        ),
        output_format=output_format,
    )


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
        _github(
            person,
            lambda service: service.pr_review_comment(
                pr_url, body, file_path, line, side, start_line, start_side
            ),
            evidence="pr_review_comment",
        ),
        output_format=output_format,
    )


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
        _github(
            person,
            lambda service: service.pr_reply(pr_url, reply_target_id, body),
            evidence="pr_reply",
        ),
        output_format=output_format,
    )


@github.group()
def run() -> None:
    """GitHub Actions run operations."""


@run.group()
def artifact() -> None:
    """GitHub Actions artifact operations."""


@artifact.command(name="download")
@_read_only_member_command
@_person_option
@click.option(
    "--url",
    "target_url",
    required=True,
    help="Pull request URL or GitHub Actions run URL.",
)
@click.option("--name", required=True, help="Exact artifact name.")
@click.option(
    "--dest",
    type=_HostPath(file_okay=False),
    default=Path("."),
    help=(
        "Directory to extract into. Defaults to the current directory. Remove "
        "downloaded files after inspection when this is inside a repository."
    ),
)
@_json_format_option
def artifact_download(
    person: str,
    target_url: str,
    name: str,
    dest: Path,
    output_format: str,
) -> None:
    _run(
        _github(
            person,
            lambda service: service.artifact_download(target_url, name, dest),
        ),
        output_format=output_format,
    )


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
    context, member_person = _resolve(person)
    store = TaskRunStore()
    try:
        readiness: list[dict[str, Any]] = []
        if status == "done":
            service = MemberGitHubCapabilityService(member_person, context.team)
            try:
                readiness = await service.task_completion_readiness(
                    ticket_url, store.evidence(run_id)
                )
            finally:
                await service.aclose()
        payload = store.complete(run_id, status, summary, ticket_url, person).to_dict()
        if status == "done":
            payload["pr_readiness"] = readiness
        return payload
    except (MemberCapabilityError, TaskRunError) as exc:
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
    except PersonExecutionNotAllowedError as exc:
        raise click.ClickException(str(exc)) from exc
    except PersonNotFoundError as exc:
        message = f"Unknown member '{exc.identifier}'."
        if exc.available:
            message = f"{message} Available members: {', '.join(exc.available)}."
        raise click.ClickException(message) from exc


def _read_stdin(label: str, *, allow_empty: bool = False) -> str:
    context = click.get_current_context(silent=True)
    text = (
        context.meta[_CONTENT_META_KEY]
        if context is not None and _CONTENT_META_KEY in context.meta
        else _read_call_stdin()
    )
    if text.strip():
        return text
    if allow_empty:
        return ""
    raise click.ClickException(f"{label} must not be empty.")


def _read_call_stdin() -> str:
    stdin = _call().stdin
    return click.get_text_stream("stdin").read() if stdin is None else stdin


def _read_content_source(content_file: Path | None) -> str:
    if content_file is None:
        return _read_call_stdin()
    try:
        return content_file.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise click.ClickException(
            t("cli.member.content.file_not_utf8", path=content_file)
        ) from exc
    except OSError as exc:
        raise click.ClickException(
            t("cli.member.content.file_read_failed", path=content_file, error=exc)
        ) from exc


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
    needs_sync = _member_command_needs_lease()
    prepared_sync = None
    started = False
    try:
        with _member_execution_guard(command, interactive_session):
            if needs_sync:
                prepared_sync = _prepare_member_sync()
            started = True
            if interactive_session is None:
                result = _run_in_owner_trace(coro, command)
            else:
                result = _run_interactive(coro, interactive_session, command)
    except (
        MemberCapabilityError,
        MemberMemoryError,
        ChatUpdatesRequired,
        TaskRunError,
        KeyError,
    ) as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    except SyncRepositoryBusyError as exc:
        raise click.ClickException(str(exc)) from exc
    except BaseException:
        if not started and asyncio.iscoroutine(coro):
            coro.close()
        raise
    if needs_sync:
        result = _sync_member_result(result, prepared_sync)
    _emit(result, output_format)
    return result


def run_in_process(
    arguments: Sequence[str],
    invocation: MemberInvocation,
    *,
    cwd: Path,
    stdin: str,
) -> tuple[int, str, str]:
    """Run one ``guildbotics member`` command in this process, as the CLI would.

    The member broker runs its turn's commands here instead of starting a CLI
    process for each. The command gets its own working directory, streams,
    and invocation, so commands running at once on other threads never see
    each other's, and the process's own are left alone.

    Args:
        arguments: The command's tokens after ``member``.
        invocation: The asking turn's metadata and execution lease.
        cwd: The directory relative paths are read from.
        stdin: What ``--content-stdin`` reads.

    Returns:
        The exit code, standard output, and standard error of the command.
    """
    stdout, stderr = io.StringIO(), io.StringIO()
    call = MemberCall(cwd=cwd, stdin=stdin, stdout=stdout)
    try:
        with (
            member_invocation_scope(invocation),
            member.make_context("guildbotics member", list(arguments), obj=call) as ctx,
        ):
            member.invoke(ctx)
        exit_code = 0
    except click.exceptions.Exit as exc:
        exit_code = exc.exit_code
    except click.ClickException as exc:
        exc.show(stderr)
        exit_code = exc.exit_code
    except Exception:
        # What an uncaught error prints and returns when the CLI runs alone.
        stderr.write(traceback.format_exc())
        exit_code = 1
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _prepare_member_sync() -> PreparedOneShotSync | None:
    """Prepare one-shot sync and turn identity damage into an actionable error."""
    try:
        return prepare_commit_and_push_once()
    except ValidationError as exc:
        identity_paths = {
            WorkspaceIdentity.__name__: workspace_identity_path(),
            DeviceIdentity.__name__: device_identity_path(),
        }
        path = identity_paths.get(exc.title)
        if path is None:
            raise
        raise click.ClickException(
            t("cli.member.sync.invalid_identity", path=path)
        ) from exc


def _sync_member_result(
    result: dict[str, Any], prepared_sync: PreparedOneShotSync | None
) -> dict[str, Any]:
    """Make one best-effort sync and expose a local lock timeout in output."""
    if prepared_sync is None:
        return result
    try:
        status = prepared_sync.commit_and_push_once(
            timeout=ONE_SHOT_LOCK_TIMEOUT_SECONDS
        )
    except SyncRepositoryBusyError:
        return {**result, "sync": "pending"}
    if status.failure is not None:
        return {**result, "sync": "pending"}
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
    if _running_under_workflow():
        # The workflow's turn holds the person's lease; its commands act under it.
        lease = current_member_invocation().lease
        if lease is None or (
            lease.person_id != person
            and lease.person_id != _resolve(person)[1].person_id
        ):
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
    """Run one member command inside its interactive session.

    The command's start and end are local diagnostics; what the session did
    is one shared record, rewritten when the command ends, so other devices
    read the session from that record instead of from its command events.
    """
    store = InteractiveTraceStore()
    recorded: dict[str, Any] = {}

    def _observe(item: dict[str, Any]) -> None:
        attributes = item.get("attributes")
        if isinstance(attributes, dict):
            for key, value in attributes.items():
                recorded.setdefault(str(key), value)

    status = "failed"
    try:
        with (
            trace_scope(
                "interactive",
                person_id=session.person_id,
                command=command,
                attributes=session.attributes,
                trace_id=session.trace_id,
            ),
            diagnostics_record_scope(_observe),
        ):
            _record_member_command_event("member.command.started", command)
            try:
                result = asyncio.run(coro)
            except BaseException as exc:
                # Ctrl-C ends the session's command too, and it is classified
                # like any other command failure so the session that recorded
                # the start also records its end instead of reading as running.
                _record_member_command_event(
                    "member.command.failed",
                    command,
                    command_failure_payload(exc),
                )
                raise
            _record_member_command_event("member.command.finished", command)
            status = "success"
            return cast(dict[str, Any], result)
    finally:
        store.touch(session)
        _record_interactive_session(
            session, command=command, status=status, attributes=recorded
        )


def _record_interactive_session(
    session: InteractiveTraceSession,
    *,
    command: str,
    status: str,
    attributes: dict[str, Any],
) -> None:
    """Rewrite the session's shared record; recording never fails the command."""
    with suppress(OSError, SharedWriteBusyError):
        InteractiveSessionStore().record(
            session, command=command, status=status, attributes=attributes
        )


def _run_in_owner_trace(coro, command: str) -> Any:
    """Run a command an agent turn asked for inside that turn's trace.

    The broker hands the trace over in the invocation; without it (a plain
    workflow-less invocation) the command records on its own.
    """
    trace_id = current_member_invocation().trace_id
    if not trace_id:
        return asyncio.run(coro)
    person = _current_person()
    person_id = _resolve(person)[1].person_id if person else ""
    with join_trace(trace_id, person_id=person_id, command=command):
        return asyncio.run(coro)


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
    return InteractiveTraceStore().start_or_touch(
        person_id=member_person.person_id,
        workspace=str(get_workspace_root()),
        host=interactive_host(),
        thread_key=interactive_thread_key(),
    )


def _running_under_workflow() -> bool:
    invocation = current_member_invocation()
    return bool(invocation.task_run_id or invocation.run_id)


def _current_person() -> str:
    ctx = click.get_current_context(silent=True)
    while ctx is not None:
        value = ctx.params.get("person") if ctx.params else None
        if isinstance(value, str) and value:
            return value
        ctx = ctx.parent
    return ""


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
        click.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            file=_call().stdout,
        )
        return
    click.echo(_to_markdown(payload), file=_call().stdout)


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
        if key == "failed_logs" and isinstance(value, list):
            lines.append("## Failed job logs")
            if not value:
                lines.append("_No failed GitHub Actions job logs found._")
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "Unnamed job")
                conclusion = str(item.get("conclusion") or "unknown")
                lines.append(f"### {name} ({conclusion})")
                metadata = {
                    item_key: item_value
                    for item_key, item_value in item.items()
                    if item_key != "log"
                }
                lines.append(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
                log = str(item.get("log") or "")
                longest_ticks = max(
                    (len(match.group(0)) for match in re.finditer(r"`+", log)),
                    default=0,
                )
                fence = "`" * max(3, longest_ticks + 1)
                lines.extend((f"{fence}text", log.rstrip("\n"), fence))
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
