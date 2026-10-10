"""The member's work on the configured code host: issues, pull requests,
reviews, reactions, CI, and the readiness a run is completed against.

Each operation is the code host's (:class:`CodeHostingService`); here are only
the rules that hold whichever code host it is -- what needs a human's
approval, that a write waits for the chat the run answers, how a result reads
-- and the result payloads the member CLI prints.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from shutil import copyfileobj
from typing import IO, Any, get_args

from pydantic import TypeAdapter, ValidationError

from guildbotics.capabilities.artifact_archive import (
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    Unpacked,
    extract_artifact,
    unpacked,
)
from guildbotics.capabilities.chat_updates import ensure_chat_current
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.runtime.code_hosting_resources import (
    MAX_PAGE_BYTES,
    RepositoryReadError,
)
from guildbotics.runtime.code_hosting_service import (
    CodeHostingService,
    ReactionTarget,
    ReviewEvent,
)
from guildbotics.runtime.context import Context
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.runtime.member_invocation import (
    GuestProcessError,
    current_member_invocation,
)
from guildbotics.utils.fileio import host_temporary_directory
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

_SIDES = {"LEFT", "RIGHT"}


async def read_repository(
    context: Context,
    resource: str,
    repo: str,
    *,
    identifier: str,
    parameters: dict[str, Any],
    continuation: str,
) -> dict[str, Any]:
    page = await context.get_code_hosting_service().read(
        resource,
        repo,
        identifier=identifier,
        parameters=parameters,
        continuation=continuation,
    )
    result = page.model_dump()
    if page.target is None:
        result.pop("target")
    if len(json.dumps(result).encode()) > MAX_PAGE_BYTES:
        raise RepositoryReadError(t("integrations.repository.too_large"))
    return result


async def issue_comment(context: Context, url: str, body: str) -> dict[str, Any]:
    code = _writing(context)
    written = await code.comment(url, body, kind="issue")
    return {
        **written.model_dump(exclude={"target"}),
        "comment_url": written.html_url,
        "issue_number": written.target.number,
        "repo": written.target.repo,
        "issue_url": written.target.html_url,
        "target": written.target.model_dump(),
    }


async def issue_create(
    context: Context,
    repo: str,
    title: str,
    body: str,
    add_to_project: bool,
    labels: Sequence[str] = (),
    human_approved: bool = False,
) -> dict[str, Any]:
    _require_human_approval(human_approved, "Creating an issue")
    created = await _writing(context).create_issue(repo, title, body, _cleaned(labels))
    project_item_id = None
    if add_to_project and context.team.project.is_available_service(
        Service.TICKET_MANAGER
    ):
        project_item_id = await context.get_ticket_manager().add_ticket(
            created.issue_url
        )
    return {
        **created.model_dump(exclude={"target"}),
        "project_item_id": project_item_id,
        "target": created.target.model_dump(),
    }


async def issue_update(
    context: Context,
    url: str,
    body: str | None = None,
    title: str | None = None,
    add_labels: Sequence[str] = (),
    remove_labels: Sequence[str] = (),
    state: str | None = None,
    state_reason: str | None = None,
    human_approved: bool = False,
) -> dict[str, Any]:
    if state is not None:
        _require_human_approval(
            human_approved, f"Changing the issue state to '{state}'"
        )
    add_labels, remove_labels = _cleaned(add_labels), _cleaned(remove_labels)
    if (
        body is None
        and title is None
        and state is None
        and not (add_labels or remove_labels)
    ):
        raise MemberCapabilityError(
            "issue update needs at least one of body, title, labels, or state."
        )
    updated = await _writing(context).update_issue(
        url,
        body=body,
        title=title,
        add_labels=add_labels,
        remove_labels=remove_labels,
        state=state,
        state_reason=state_reason,
    )
    return updated.model_dump()


async def pr_create(
    context: Context,
    repo: str,
    head: str,
    base: str,
    title: str,
    body: str,
    issue_url: str,
    draft: str,
    closes_issue: bool = False,
) -> dict[str, Any]:
    created = await _writing(context).create_pull_request(
        repo,
        head,
        base,
        title,
        body,
        draft=draft == "true",
        issue_url=issue_url,
        closes_issue=closes_issue,
    )
    return created.model_dump()


async def pr_update(
    context: Context,
    url: str,
    body: str | None = None,
    title: str | None = None,
    *,
    drop_issue_links: bool = False,
) -> dict[str, Any]:
    if drop_issue_links and body is None:
        raise MemberCapabilityError("--drop-issue-links requires a body.")
    if body is None and title is None:
        raise MemberCapabilityError("pr update needs a body or a title.")
    updated = await _writing(context).update_pull_request(
        url, body=body, title=title, drop_issue_links=drop_issue_links
    )
    return updated.model_dump()


async def pr_comment(context: Context, url: str, body: str) -> dict[str, Any]:
    written = await _writing(context).comment(url, body, kind="pull_request")
    return written.model_dump()


async def pr_review(
    context: Context, url: str, body: str, event: ReviewEvent
) -> dict[str, Any]:
    if event not in get_args(ReviewEvent):
        raise MemberCapabilityError(
            "Review event must be one of " + ", ".join(get_args(ReviewEvent)) + "."
        )
    return (await _writing(context).review(url, body, event)).model_dump()


async def pr_review_comment(
    context: Context,
    url: str,
    body: str,
    path: str,
    line: int,
    side: str,
    start_line: int | None,
    start_side: str | None,
) -> dict[str, Any]:
    _validate_review_comment_location(path, line, side, start_line, start_side)
    written = await _writing(context).review_comment(
        url, body, path, line, side, start_line, start_side
    )
    return written.model_dump()


async def pr_reply(
    context: Context, url: str, reply_target_id: int, body: str
) -> dict[str, Any]:
    written = await _writing(context).reply(url, reply_target_id, body)
    return written.model_dump()


async def reaction_add(
    context: Context,
    repo: str,
    target: ReactionTarget,
    comment_id: int,
    reaction: str,
    pr_number: int | None = None,
) -> dict[str, Any]:
    added = await _writing(context).add_reaction(
        repo, target, comment_id, reaction, pr_number
    )
    return added.model_dump()


async def artifact_download(
    context: Context, url: str, name: str, destination: Path
) -> dict[str, Any]:
    """Download and safely extract one CI artifact."""
    name = name.strip()
    if not name:
        raise MemberCapabilityError("Artifact name is required.")
    code = context.get_code_hosting_service()
    async with code.artifact(url, name, MAX_ARTIFACT_BYTES) as (ref, archive):
        where = _unpack(archive, destination)
    return {**ref.model_dump(), **where}


async def open_pull_request_readiness(
    code: CodeHostingService, remote_url: str, branch: str
) -> list[dict[str, Any]]:
    """Readiness of the open pull requests whose head is the pushed branch."""
    results = []
    for url in await code.open_pull_requests(remote_url, branch):
        checks = await code.readiness(url)
        if checks.readiness == "not_applicable":
            continue
        results.append(
            checks.model_dump(include={"pr_url", "readiness", "completion_blockers"})
        )
    return results


async def task_completion_readiness(
    context: Context, ticket_url: str, evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Revalidate every PR a ticket run is answerable for before completion.

    PRs the run pushed to or opened (evidence) are always targets. The
    ticket itself, when it is a PR, and PRs linked from an issue ticket
    are targets only when the member authored them: a reviewer cannot
    make someone else's PR ready.

    Raises:
        MemberCapabilityError: If a target is not ready.
    """
    code = context.get_code_hosting_service()
    touched = _pull_request_urls(code, evidence)
    try:
        ticket = code.locate(ticket_url)
    except (MemberCapabilityError, ValueError):
        ticket = None
    urls = [ticket.url] if ticket is not None and ticket.kind == "pull_request" else []
    urls.extend(touched)
    if ticket is not None and ticket.kind == "issue":
        urls.extend(await code.linked_pull_requests(ticket_url))
    results = []
    for url in dict.fromkeys(urls):
        ref = code.locate(url, "pull_request")
        page = await code.read(
            "pull_requests", ref.full_repo, identifier=str(ref.number)
        )
        pr = page.items[0]
        if pr["state"] != "open" or (
            url not in touched and pr["author_type"] != Message.ASSISTANT
        ):
            continue
        result = await code.readiness(url)
        if result.readiness != "not_applicable":
            results.append(result.model_dump())
    blocked = [result for result in results if result["readiness"] != "ready"]
    if blocked:
        details = "; ".join(
            f"{result['pr_url']}: "
            + ", ".join(
                str(blocker["message"]) for blocker in result["completion_blockers"]
            )
            for result in blocked
        )
        raise MemberCapabilityError(
            "Task cannot be completed because PR readiness is blocked. " + details
        )
    return results


def _pull_request_urls(
    code: CodeHostingService, evidence: list[dict[str, Any]]
) -> list[str]:
    """The pull requests a run's evidence names, as the code host spells them."""
    candidates: list[str] = []
    for record in evidence:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("pr_url", "html_url"):
            value = payload.get(key)
            if isinstance(value, str):
                candidates.append(value)
        pull_requests = payload.get("pull_requests")
        if isinstance(pull_requests, list):
            candidates.extend(
                str(item.get("pr_url") or "")
                for item in pull_requests
                if isinstance(item, dict)
            )
    urls: list[str] = []
    for candidate in candidates:
        try:
            url = code.locate(candidate, "pull_request").url
        except (MemberCapabilityError, ValueError):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def _writing(context: Context) -> CodeHostingService:
    """The code host, once the chat the run answers is current: a write made
    without reading what was said since would answer an old conversation."""
    ensure_chat_current(context.person.person_id)
    return context.get_code_hosting_service()


def _cleaned(labels: Sequence[str]) -> list[str]:
    return [name for name in (label.strip() for label in labels) if name]


def _require_human_approval(human_approved: bool, action: str) -> None:
    """Guard the writes that stay a human decision.

    Opening and closing issues shape the team's backlog, so the member may only
    perform them on a human's instruction or approval. The flag is the member's
    attestation that such an instruction exists in the originating conversation.
    """
    if not human_approved:
        raise MemberCapabilityError(
            f"{action} requires a human instruction or approval. "
            "Ask the human first, then re-run with --human-approved."
        )


def _validate_review_comment_location(
    path: str,
    line: int,
    side: str,
    start_line: int | None,
    start_side: str | None,
) -> None:
    if not path.strip():
        raise MemberCapabilityError("Review comment path is required.")
    if line < 1:
        raise MemberCapabilityError("Review comment line must be positive.")
    if side not in _SIDES:
        raise MemberCapabilityError("Review comment side must be LEFT or RIGHT.")
    if start_side is not None and start_side not in _SIDES:
        raise MemberCapabilityError("Review comment start_side must be LEFT or RIGHT.")
    if (start_line is None) != (start_side is None):
        raise MemberCapabilityError(
            "Review comment range requires both start_line and start_side."
        )
    if start_line is not None and start_line < 1:
        raise MemberCapabilityError("Review comment start_line must be positive.")
    if start_side is not None and start_side != side:
        raise MemberCapabilityError("Review comment range start_side must match side.")
    if start_line is not None and start_line > line:
        raise MemberCapabilityError(
            "Review comment range start_line must be less than or equal to line."
        )


_UNPACKED = TypeAdapter(Unpacked)


def _unpack(archive: IO[bytes], destination: Path) -> Unpacked:
    """Unpack ``archive`` where the command that asked for it reads it: on
    the host, or, for a command of an isolated environment, in that
    environment (:mod:`.artifact_archive`), which the host writes nothing of.

    Raises:
        MemberCapabilityError: When the artifact cannot be unpacked safely,
            or the environment could not unpack it.
    """
    guest = current_member_invocation().guest
    if guest is None:
        try:
            return unpacked(destination, extract_artifact(archive, destination))
        except ArtifactError as exc:
            raise MemberCapabilityError(str(exc)) from exc
    # A process that ran out of time may still hold the copy open (on Windows
    # it cannot be deleted then); the failure is what the command reports.
    with host_temporary_directory("guildbotics-artifact-") as held:
        copy = Path(held) / "artifact.zip"
        with copy.open("wb") as file:
            copyfileobj(archive, file)
        try:
            result = guest.run(
                guest.python(
                    "guildbotics.capabilities.artifact_archive",
                    guest.path(destination),
                ),
                cwd="/",
                env={},
                stdin=copy,
                stdout_limit=STREAM_READ_LIMIT,
            )
        except GuestProcessError as exc:
            raise MemberCapabilityError(str(exc)) from exc
    if result.returncode != 0:
        raise MemberCapabilityError(
            result.stderr.decode(errors="replace").strip()
            or "The artifact could not be unpacked."
        )
    # What the environment wrote is its own to write: read as nothing more
    # than where it says it unpacked.
    try:
        return _UNPACKED.validate_json(result.stdout)
    except ValidationError as exc:
        raise MemberCapabilityError(
            "The environment reported the artifact unpacked in a form it cannot"
            " have been."
        ) from exc
