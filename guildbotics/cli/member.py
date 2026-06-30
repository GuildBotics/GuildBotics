from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlparse

import click

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
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.capabilities.task_runs import (
    RunStore,
    TaskRunError,
    TaskRunStore,
    current_run_id,
    current_task_run_id,
)
from guildbotics.commands.errors import (
    PersonExecutionNotAllowedError,
    PersonNotFoundError,
)
from guildbotics.runtime.member_context import resolve_member_context
from guildbotics.utils.env_loader import load_guildbotics_env
from guildbotics.utils.workspace_state import apply_workspace_for_cli

FormatChoice = click.Choice(["json", "markdown"])
WorkspaceMode = Literal["member", "current"]
SLACK_TS_FRACTION_DIGITS = 6


@click.group()
@click.option(
    "--workspace",
    "workspace_dir",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Workspace root to use instead of the persisted active workspace.",
)
def member(workspace_dir: Path | None) -> None:
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


@member.command(name="context")
@click.option("--person", required=True, help="Person ID or name.")
@click.option("--check-credentials", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
def context_cmd(person: str, check_credentials: bool, output_format: str) -> None:
    """Show non-secret member context."""
    _run(
        _context_cmd(person, check_credentials, output_format),
        output_format=output_format,
    )


@member.command(name="help")
def help_cmd() -> None:
    """Print the member capability reference (commands and cross-cutting rules).

    This is the same reference embedded in ``member context``; use it to reread
    the available commands without re-running the full context.
    """
    click.echo(capability_reference_text())


@member.group()
def memory() -> None:
    """Record, recall, and maintain member memory documents."""


@memory.command(name="record")
@click.option("--person", required=True)
@click.option(
    "--scope",
    type=click.Choice(["personal", "team"]),
    default="personal",
    show_default=True,
)
@click.option("--title", required=True)
@click.option("--summary", default="")
@click.option("--keyword", "keywords", multiple=True)
@click.option("--ticket", "tickets", multiple=True)
@click.option("--pr", "prs", multiple=True)
@click.option("--channel", "channels", multiple=True)
@click.option("--thread", "threads", multiple=True)
@click.option("--kind", type=click.Choice(["note", "policy"]), default="note")
@click.option("--pin", "pinned", is_flag=True)
@click.option("--body-file", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--body-stdin", is_flag=True)
@click.option("--policy-approved", is_flag=True)
@click.option("--set", "set_values", multiple=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
    body_file: Path | None,
    body_stdin: bool,
    policy_approved: bool,
    set_values: tuple[str, ...],
    output_format: str,
) -> None:
    body = _read_content(body_file, body_stdin, "body")
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
@click.option("--person", required=True)
@click.option("--query", "queries", multiple=True)
@click.option("--meta-only", is_flag=True)
@click.option("--limit", type=click.IntRange(1, 200), default=20)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option("--id", "doc_id", required=True)
@click.option("--team", "team_scope", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def memory_get(person: str, doc_id: str, team_scope: bool, output_format: str) -> None:
    _run(
        _memory_get(person, doc_id, "team" if team_scope else None),
        output_format=output_format,
    )


async def _memory_get(person: str, doc_id: str, scope: str | None) -> dict[str, Any]:
    _context, member_person = _resolve(person)
    return MemberMemoryService(member_person).get(doc_id=doc_id, scope=cast(Any, scope))


@memory.command(name="update")
@click.option("--person", required=True)
@click.option("--id", "doc_id", required=True)
@click.option("--team", "team_scope", is_flag=True)
@click.option("--title")
@click.option("--summary")
@click.option("--keyword", "keywords", multiple=True)
@click.option("--add-keyword", "add_keywords", multiple=True)
@click.option("--remove-keyword", "remove_keywords", multiple=True)
@click.option("--ticket", "tickets", multiple=True)
@click.option("--pr", "prs", multiple=True)
@click.option("--channel", "channels", multiple=True)
@click.option("--thread", "threads", multiple=True)
@click.option("--pin", "pin_value", flag_value=True, default=None)
@click.option("--unpin", "pin_value", flag_value=False)
@click.option("--kind", type=click.Choice(["note", "policy"]))
@click.option("--body-file", type=click.Path(dir_okay=False, path_type=Path))
@click.option("--body-stdin", is_flag=True)
@click.option("--policy-approved", is_flag=True)
@click.option("--set", "set_values", multiple=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
    body_file: Path | None,
    body_stdin: bool,
    policy_approved: bool,
    set_values: tuple[str, ...],
    output_format: str,
) -> None:
    body = _read_optional_content(body_file, body_stdin, "body")
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
@click.option("--person", required=True)
@click.option("--id", "doc_id", required=True)
@click.option("--team", "team_scope", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option("--id", "doc_id", required=True)
@click.option("--team", "team_scope", is_flag=True)
@click.option("--policy-approved", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option("--id", "doc_id", required=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", default="")
@click.option("--channel-name", default="")
@click.option("--oldest-ts", default="")
@click.option("--latest-ts", default="")
@click.option("--limit", type=click.IntRange(1, 200), default=50)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", default="")
@click.option("--channel-name", default="")
@click.option("--thread-ts", default="")
@click.option("--message-url", default="")
@click.option("--limit", type=click.IntRange(1, 200), default=100)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", default="")
@click.option("--channel-name", default="")
@click.option("--body-file", type=click.Path(path_type=Path))
@click.option("--body-stdin", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def chat_post(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    body_file: Path | None,
    body_stdin: bool,
    output_format: str,
) -> None:
    body = _read_content(body_file, body_stdin, "body")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", default="")
@click.option("--channel-name", default="")
@click.option("--thread-ts", default="")
@click.option("--message-url", default="")
@click.option("--body-file", type=click.Path(path_type=Path))
@click.option("--body-stdin", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def chat_reply(
    person: str,
    service_name: str,
    channel_id: str,
    channel_name: str,
    thread_ts: str,
    message_url: str,
    body_file: Path | None,
    body_stdin: bool,
    output_format: str,
) -> None:
    body = _read_content(body_file, body_stdin, "body")
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
@click.option("--person", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", default="")
@click.option("--channel-name", default="")
@click.option("--message-ts", default="")
@click.option("--message-url", default="")
@click.option(
    "--reaction",
    required=True,
    type=click.Choice(["ack", "agree", "celebrate", "support"]),
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option("--run-id", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", required=True)
@click.option("--thread-ts", required=True)
@click.option("--event-id", required=True)
@click.option("--reason-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def chat_noop(
    person: str,
    run_id: str,
    service_name: str,
    channel_id: str,
    thread_ts: str,
    event_id: str,
    reason_file: Path,
    output_format: str,
) -> None:
    reason = _read_file(reason_file, "reason-file")
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
@click.option("--person", required=True)
@click.option("--run-id", required=True)
@click.option(
    "--service", "service_name", type=click.Choice(["slack"]), default="slack"
)
@click.option("--channel-id", required=True)
@click.option("--thread-ts", required=True)
@click.option("--event-id", required=True)
@click.option(
    "--status", required=True, type=click.Choice(["done", "asking", "blocked"])
)
@click.option("--summary-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def chat_complete(
    person: str,
    run_id: str,
    service_name: str,
    channel_id: str,
    thread_ts: str,
    event_id: str,
    status: str,
    summary_file: Path,
    output_format: str,
) -> None:
    summary = _read_file(summary_file, "summary-file")
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
@click.option("--person", required=True)
@click.option("--issue-url", required=True)
@click.option("--pr-url", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_prepare(person: str, issue_url: str, pr_url: str, output_format: str) -> None:
    _run(
        _git_prepare(person, issue_url, pr_url or None),
        output_format=output_format,
    )


async def _git_prepare(
    person: str, issue_url: str, pr_url: str | None
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        return await service.prepare(issue_url=issue_url, pr_url=pr_url)
    finally:
        await service.aclose()


@git.command(name="commit")
@click.option("--person", required=True)
@click.option("--repo-path", required=True, type=click.Path(path_type=Path))
@click.option("--message-file", type=click.Path(path_type=Path))
@click.option(
    "--message-stdin",
    is_flag=True,
    help="Read the commit message from standard input instead of a file.",
)
@click.option(
    "--workspace-mode",
    type=click.Choice(["member", "current"]),
    default="member",
    help=(
        "Use 'member' for isolated workflow workspaces or 'current' for the "
        "repository currently open in an interactive coding session."
    ),
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_commit(
    person: str,
    repo_path: Path,
    message_file: Path | None,
    message_stdin: bool,
    workspace_mode: str,
    output_format: str,
) -> None:
    """Commit already-staged changes with the member identity.

    Stage the files you want with plain git (e.g. ``git add``) first; this
    command commits only what is staged and applies the member name/email to
    that single commit without changing the repository's git config.
    """
    message = _read_message(message_file, message_stdin, "commit message")
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
@click.option("--person", required=True)
@click.option("--repo-path", required=True, type=click.Path(path_type=Path))
@click.option(
    "--workspace-mode",
    type=click.Choice(["member", "current"]),
    default="member",
    help=(
        "Use 'member' for isolated workflow workspaces or 'current' for the "
        "repository currently open in an interactive coding session."
    ),
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
        return payload
    finally:
        await service.aclose()


@git.command(name="publish")
@click.option("--person", required=True)
@click.option("--repo-path", required=True, type=click.Path(path_type=Path))
@click.option("--message-file", type=click.Path(path_type=Path))
@click.option(
    "--message-stdin",
    is_flag=True,
    help="Read the commit message from standard input instead of a file.",
)
@click.option(
    "--workspace-mode",
    type=click.Choice(["member", "current"]),
    default="member",
    help=(
        "Use 'member' for isolated workflow workspaces or 'current' for the "
        "repository currently open in an interactive coding session."
    ),
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_publish(
    person: str,
    repo_path: Path,
    message_file: Path | None,
    message_stdin: bool,
    workspace_mode: str,
    output_format: str,
) -> None:
    """Commit already-staged changes with the member identity, then push.

    Stage the files you want with plain git (e.g. ``git add``) first; this
    commits only what is staged with the member name/email and pushes the
    branch using the member credential.
    """
    message = _read_message(message_file, message_stdin, "commit message")
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
@click.option("--person", required=True)
@click.option("--url", "issue_url", required=True)
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
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
@click.option("--person", required=True)
@click.option("--url", "issue_url", required=True)
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def issue_comment(
    person: str,
    issue_url: str,
    body_file: Path,
    output_format: str,
) -> None:
    body = _read_file(body_file, "body-file")
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
        return result
    finally:
        await service.aclose()


@issue.command(name="create")
@click.option("--person", required=True)
@click.option("--repo", required=True)
@click.option("--title-file", required=True, type=click.Path(path_type=Path))
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--add-to-project/--no-add-to-project", default=True)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def issue_create(
    person: str,
    repo: str,
    title_file: Path,
    body_file: Path,
    add_to_project: bool,
    output_format: str,
) -> None:
    title = _read_file(title_file, "title-file")
    body = _read_file(body_file, "body-file")
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
        return result
    finally:
        await service.aclose()


@github.group()
def pr() -> None:
    """GitHub pull request operations."""


@pr.command(name="inspect")
@click.option("--person", required=True)
@click.option("--url", "pr_url", required=True)
@click.option("--include-comments", is_flag=True)
@click.option("--format", "output_format", type=FormatChoice, default="markdown")
def pr_inspect(
    person: str, pr_url: str, include_comments: bool, output_format: str
) -> None:
    _run(
        _pr_inspect(person, pr_url, include_comments),
        output_format=output_format,
    )


async def _pr_inspect(
    person: str, pr_url: str, include_comments: bool
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        return await service.pr_inspect(pr_url, include_comments)
    finally:
        await service.aclose()


@pr.command(name="create")
@click.option("--person", required=True)
@click.option("--repo", required=True)
@click.option("--head", required=True)
@click.option(
    "--base",
    default="",
    help="Base branch for the pull request. Defaults to the repository default branch.",
)
@click.option("--title-file", type=click.Path(path_type=Path))
@click.option("--body-file", type=click.Path(path_type=Path))
@click.option(
    "--content-stdin",
    is_flag=True,
    help=(
        "Read PR title and body from standard input. The first line is the "
        "title; the remaining content is the body."
    ),
)
@click.option("--issue-url", default="")
@click.option("--draft", type=click.Choice(["auto", "true", "false"]), default="auto")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def pr_create(
    person: str,
    repo: str,
    head: str,
    base: str,
    title_file: Path | None,
    body_file: Path | None,
    content_stdin: bool,
    issue_url: str,
    draft: str,
    output_format: str,
) -> None:
    title, body = _read_pr_content(title_file, body_file, content_stdin)
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
        return result
    finally:
        await service.aclose()


@pr.command(name="comment")
@click.option("--person", required=True)
@click.option("--url", "pr_url", required=True)
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def pr_comment(person: str, pr_url: str, body_file: Path, output_format: str) -> None:
    body = _read_file(body_file, "body-file")
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


@pr.command(name="reply")
@click.option("--person", required=True)
@click.option("--url", "pr_url", required=True)
@click.option("--reply-target-id", required=True, type=int)
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def pr_reply(
    person: str,
    pr_url: str,
    reply_target_id: int,
    body_file: Path,
    output_format: str,
) -> None:
    body = _read_file(body_file, "body-file")
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
@click.option("--person", required=True)
@click.option("--repo", required=True)
@click.option(
    "--target",
    required=True,
    type=click.Choice(["issue-comment", "pr-review-comment"]),
)
@click.option("--comment-id", required=True, type=int)
@click.option(
    "--reaction",
    "reaction_content",
    required=True,
    type=click.Choice(
        ["+1", "eyes", "heart", "hooray", "rocket", "laugh", "confused", "-1"]
    ),
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
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
@click.option("--person", required=True)
@click.option("--run-id", required=True)
@click.option("--ticket-url", required=True)
@click.option(
    "--status", required=True, type=click.Choice(["done", "asking", "blocked"])
)
@click.option("--summary-file", required=True, type=click.Path(path_type=Path))
@click.option("--format", "output_format", type=FormatChoice, default="json")
def task_complete(
    person: str,
    run_id: str,
    ticket_url: str,
    status: str,
    summary_file: Path,
    output_format: str,
) -> None:
    summary = _read_file(summary_file, "summary-file")
    _resolve(person)
    store = TaskRunStore()
    try:
        payload = store.complete(run_id, status, summary, ticket_url, person).to_dict()
    except TaskRunError as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    _emit(payload, output_format)


@task.command(name="status")
@click.option("--run-id", required=True)
@click.option(
    "--person",
    default="",
    help="Accepted for consistency with other member commands; not required.",
)
@click.option("--format", "output_format", type=FormatChoice, default="json")
def task_status(run_id: str, person: str, output_format: str) -> None:
    del person
    try:
        payload = TaskRunStore().status(run_id).to_dict()
    except TaskRunError as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    _emit(payload, output_format)


def _resolve(person: str):
    try:
        return resolve_member_context(person)
    except PersonNotFoundError as exc:
        message = f"Unknown member '{exc.identifier}'."
        if exc.available:
            message = f"{message} Available members: {', '.join(exc.available)}."
        raise click.ClickException(message) from exc


def _read_file(path: Path, label: str) -> str:
    if not path.is_file():
        raise click.ClickException(f"{label} does not exist or is not a file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise click.ClickException(f"{label} must not be empty: {path}")
    return text


def _read_message(message_file: Path | None, message_stdin: bool, label: str) -> str:
    if message_file is not None and message_stdin:
        raise click.ClickException(
            "Use either --message-file or --message-stdin, not both."
        )
    if message_file is not None:
        return _read_file(message_file, "message-file")
    if message_stdin:
        text = click.get_text_stream("stdin").read()
        if not text.strip():
            raise click.ClickException(f"{label} must not be empty.")
        return text
    raise click.ClickException("Either --message-file or --message-stdin is required.")


def _read_content(content_file: Path | None, content_stdin: bool, label: str) -> str:
    if content_file is not None and content_stdin:
        raise click.ClickException(
            f"Use either --{label}-file or --{label}-stdin, not both."
        )
    if content_file is not None:
        return _read_file(content_file, f"{label}-file")
    if content_stdin:
        text = click.get_text_stream("stdin").read()
        if not text.strip():
            raise click.ClickException(f"{label} must not be empty.")
        return text
    raise click.ClickException(f"Either --{label}-file or --{label}-stdin is required.")


def _read_optional_content(
    content_file: Path | None, content_stdin: bool, label: str
) -> str | None:
    if content_file is None and not content_stdin:
        return None
    return _read_content(content_file, content_stdin, label)


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


def _read_pr_content(
    title_file: Path | None, body_file: Path | None, content_stdin: bool
) -> tuple[str, str]:
    if content_stdin and (title_file is not None or body_file is not None):
        raise click.ClickException(
            "Use either --content-stdin or both --title-file and --body-file, not both."
        )
    if content_stdin:
        text = click.get_text_stream("stdin").read()
        title, separator, body = text.partition("\n")
        if not separator:
            raise click.ClickException(
                "--content-stdin requires a title line and body content."
            )
        body = body.removeprefix("\n")
        if not title.strip():
            raise click.ClickException("PR title must not be empty.")
        if not body.strip():
            raise click.ClickException("PR body must not be empty.")
        return title.strip(), body
    if title_file is not None and body_file is not None:
        return _read_file(title_file, "title-file"), _read_file(body_file, "body-file")
    raise click.ClickException(
        "Either --content-stdin or both --title-file and --body-file is required."
    )


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
    try:
        result = asyncio.run(coro)
    except (MemberCapabilityError, MemberMemoryError, TaskRunError, KeyError) as exc:
        raise click.ClickException(_safe_error(exc)) from exc
    _emit(result, output_format)
    return result


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
