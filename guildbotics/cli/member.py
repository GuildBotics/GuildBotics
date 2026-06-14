from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast

import click

from guildbotics.capabilities.member_git import MemberGitWorkspaceService
from guildbotics.capabilities.member_github import (
    MemberCapabilityError,
    MemberGitHubCapabilityService,
)
from guildbotics.capabilities.task_runs import (
    TaskRunError,
    TaskRunStore,
    current_task_run_id,
)
from guildbotics.commands.errors import PersonNotFoundError
from guildbotics.runtime.member_context import resolve_member_context
from guildbotics.utils.env_loader import load_guildbotics_env
from guildbotics.utils.workspace_state import apply_workspace_for_cli

FormatChoice = click.Choice(["json", "markdown"])
WorkspaceMode = Literal["member", "current"]


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
        apply_workspace_for_cli(workspace_dir)
    except NotADirectoryError as exc:
        raise click.ClickException(f"workspace does not exist: {exc}") from exc
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


async def _context_cmd(
    person: str, check_credentials: bool, output_format: str
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        return await service.context(check_credentials=check_credentials)
    finally:
        await service.aclose()


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
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_commit(
    person: str,
    repo_path: Path,
    message_file: Path | None,
    message_stdin: bool,
    workspace_mode: str,
    run_id: str,
    output_format: str,
) -> None:
    message = _read_message(message_file, message_stdin, "commit message")
    _run(
        _git_commit(person, repo_path, message, workspace_mode, run_id or None),
        output_format=output_format,
    )


async def _git_commit(
    person: str,
    repo_path: Path,
    message: str,
    workspace_mode: str,
    run_id: str | None,
) -> dict[str, Any]:
    task_run_id = current_task_run_id(run_id)
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
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_push(
    person: str,
    repo_path: Path,
    workspace_mode: str,
    run_id: str,
    output_format: str,
) -> None:
    _run(
        _git_push(person, repo_path, workspace_mode, run_id or None),
        output_format=output_format,
    )


async def _git_push(
    person: str,
    repo_path: Path,
    workspace_mode: str,
    run_id: str | None,
) -> dict[str, Any]:
    task_run_id = current_task_run_id(run_id)
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


@git.group(name="branch")
def git_branch() -> None:
    """Manage git branches as a configured member."""


@git_branch.command(name="create")
@click.option("--person", required=True)
@click.option("--repo-path", required=True, type=click.Path(path_type=Path))
@click.option("--branch", required=True)
@click.option(
    "--workspace-mode",
    type=click.Choice(["member", "current"]),
    default="member",
    help=(
        "Use 'member' for isolated workflow workspaces or 'current' for the "
        "repository currently open in an interactive coding session."
    ),
)
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_branch_create(
    person: str,
    repo_path: Path,
    branch: str,
    workspace_mode: str,
    run_id: str,
    output_format: str,
) -> None:
    _run(
        _git_branch_create(person, repo_path, branch, workspace_mode, run_id or None),
        output_format=output_format,
    )


async def _git_branch_create(
    person: str,
    repo_path: Path,
    branch: str,
    workspace_mode: str,
    run_id: str | None,
) -> dict[str, Any]:
    task_run_id = current_task_run_id(run_id)
    _reject_current_workspace_mode_in_task_run(workspace_mode, task_run_id)
    context, member_person = _resolve(person)
    service = MemberGitWorkspaceService(member_person, context.team, context.logger)
    try:
        result = await service.create_branch(
            repo_path=repo_path,
            branch=branch,
            workspace_mode=_workspace_mode(workspace_mode),
            cwd=Path.cwd(),
        )
        payload = result.to_dict()
        TaskRunStore().append_evidence(task_run_id, "git_branch_create", payload)
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
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def git_publish(
    person: str,
    repo_path: Path,
    message_file: Path | None,
    message_stdin: bool,
    workspace_mode: str,
    run_id: str,
    output_format: str,
) -> None:
    message = _read_message(message_file, message_stdin, "commit message")
    result = _run(
        _git_publish(person, repo_path, message, workspace_mode, run_id or None),
        output_format=output_format,
    )
    return result


async def _git_publish(
    person: str,
    repo_path: Path,
    message: str,
    workspace_mode: str,
    run_id: str | None,
) -> dict[str, Any]:
    task_run_id = current_task_run_id(run_id)
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
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def issue_comment(
    person: str,
    issue_url: str,
    body_file: Path,
    run_id: str,
    output_format: str,
) -> None:
    body = _read_file(body_file, "body-file")
    _run(
        _issue_comment(person, issue_url, body, run_id or None),
        output_format=output_format,
    )


async def _issue_comment(
    person: str, issue_url: str, body: str, run_id: str | None
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.issue_comment(issue_url, body)
        TaskRunStore().append_evidence(
            current_task_run_id(run_id), "issue_comment", result
        )
        return result
    finally:
        await service.aclose()


@issue.command(name="create")
@click.option("--person", required=True)
@click.option("--repo", required=True)
@click.option("--title-file", required=True, type=click.Path(path_type=Path))
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--add-to-project/--no-add-to-project", default=True)
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def issue_create(
    person: str,
    repo: str,
    title_file: Path,
    body_file: Path,
    add_to_project: bool,
    run_id: str,
    output_format: str,
) -> None:
    title = _read_file(title_file, "title-file")
    body = _read_file(body_file, "body-file")
    _run(
        _issue_create(person, repo, title, body, add_to_project, run_id or None),
        output_format=output_format,
    )


async def _issue_create(
    person: str,
    repo: str,
    title: str,
    body: str,
    add_to_project: bool,
    run_id: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.issue_create(repo, title, body, add_to_project)
        TaskRunStore().append_evidence(
            current_task_run_id(run_id), "issue_create", result
        )
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
@click.option("--run-id", default="")
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
    run_id: str,
    output_format: str,
) -> None:
    title, body = _read_pr_content(title_file, body_file, content_stdin)
    _run(
        _pr_create(
            person, repo, head, base, title, body, issue_url, draft, run_id or None
        ),
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
    run_id: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_create(
            repo, head, base, title, body, issue_url, draft
        )
        TaskRunStore().append_evidence(current_task_run_id(run_id), "pr_create", result)
        return result
    finally:
        await service.aclose()


@pr.command(name="comment")
@click.option("--person", required=True)
@click.option("--url", "pr_url", required=True)
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def pr_comment(
    person: str, pr_url: str, body_file: Path, run_id: str, output_format: str
) -> None:
    body = _read_file(body_file, "body-file")
    _run(
        _pr_comment(person, pr_url, body, run_id or None),
        output_format=output_format,
    )


async def _pr_comment(
    person: str, pr_url: str, body: str, run_id: str | None
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_comment(pr_url, body)
        TaskRunStore().append_evidence(
            current_task_run_id(run_id), "pr_comment", result
        )
        return result
    finally:
        await service.aclose()


@pr.command(name="reply")
@click.option("--person", required=True)
@click.option("--url", "pr_url", required=True)
@click.option("--reply-target-id", required=True, type=int)
@click.option("--body-file", required=True, type=click.Path(path_type=Path))
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def pr_reply(
    person: str,
    pr_url: str,
    reply_target_id: int,
    body_file: Path,
    run_id: str,
    output_format: str,
) -> None:
    body = _read_file(body_file, "body-file")
    _run(
        _pr_reply(person, pr_url, reply_target_id, body, run_id or None),
        output_format=output_format,
    )


async def _pr_reply(
    person: str,
    pr_url: str,
    reply_target_id: int,
    body: str,
    run_id: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.pr_reply(pr_url, reply_target_id, body)
        TaskRunStore().append_evidence(current_task_run_id(run_id), "pr_reply", result)
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
@click.option("--run-id", default="")
@click.option("--format", "output_format", type=FormatChoice, default="json")
def reaction_add(
    person: str,
    repo: str,
    target: str,
    comment_id: int,
    reaction_content: str,
    run_id: str,
    output_format: str,
) -> None:
    _run(
        _reaction_add(
            person, repo, target, comment_id, reaction_content, run_id or None
        ),
        output_format=output_format,
    )


async def _reaction_add(
    person: str,
    repo: str,
    target: str,
    comment_id: int,
    reaction_content: str,
    run_id: str | None,
) -> dict[str, Any]:
    context, member_person = _resolve(person)
    service = MemberGitHubCapabilityService(member_person, context.team)
    try:
        result = await service.reaction_add(repo, target, comment_id, reaction_content)
        TaskRunStore().append_evidence(
            current_task_run_id(run_id), "reaction_add", result
        )
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


def _run(coro, *, output_format: str) -> Any:
    try:
        result = asyncio.run(coro)
    except (MemberCapabilityError, TaskRunError, KeyError) as exc:
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
