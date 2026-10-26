"""The member's work on the configured code host and tickets: reads, pull
requests, reviews, reactions, CI, and the readiness a run is completed
against.

Each operation is a service's (:class:`CodeHostingService`,
:class:`TicketService`); here are only the rules that hold whichever service
it is -- what needs a human's approval, that a write waits for the chat the
run answers, how a result reads -- and the result payloads the member CLI
prints.
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
from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.capabilities.task_runs import RunStore, work_subject
from guildbotics.entities.message import Message
from guildbotics.integrations import pull_request_patrol
from guildbotics.runtime.code_hosting_service import CodeHostingService, TicketLink
from guildbotics.runtime.context import Context
from guildbotics.runtime.member_invocation import (
    GuestProcessError,
    Work,
    current_member_invocation,
)
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
)
from guildbotics.runtime.resources import (
    MAX_PAGE_BYTES,
    TICKET_RESOURCES,
    Readiness,
    ReviewVerdict,
)
from guildbotics.runtime.ticket_service import TicketService
from guildbotics.utils.fileio import host_temporary_directory
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

_SIDES = {"LEFT", "RIGHT"}


async def read_resource(
    context: Context,
    resource: str,
    subject: str,
    *,
    parameters: dict[str, Any],
    continuation: str,
) -> dict[str, Any]:
    """A page of ``resource`` of ``subject``, read by the service it is of."""
    service: CodeHostingService | TicketService = (
        context.get_ticket_service()
        if resource in TICKET_RESOURCES
        else context.get_code_hosting_service()
    )
    page = await service.read(
        resource, subject, parameters=parameters, continuation=continuation
    )
    result = page.model_dump()
    if page.target is None:
        result.pop("target")
    if len(json.dumps(result).encode()) > MAX_PAGE_BYTES:
        raise ServiceError(t("integrations.resources.too_large"))
    return result


async def ticket_comment(context: Context, url: str, body: str) -> dict[str, Any]:
    written = await _tickets(context).comment(url, body)
    return written.model_dump()


async def ticket_create(
    context: Context,
    tracker: str,
    title: str,
    body: str,
    add_to_board: bool,
    labels: Sequence[str] = (),
    human_approved: bool = False,
) -> dict[str, Any]:
    _require_human_approval(human_approved, "Creating a ticket")
    created = await _tickets(context).create_ticket(
        tracker, title, body, _cleaned(labels), add_to_board=add_to_board
    )
    return created.model_dump()


async def ticket_update(
    context: Context,
    url: str,
    body: str | None = None,
    title: str | None = None,
    add_labels: Sequence[str] = (),
    remove_labels: Sequence[str] = (),
    state: str | None = None,
    state_reason: str | None = None,
    human_approved: bool = False,
    parent: str | None = None,
    add_blocked_by: Sequence[str] = (),
    remove_blocked_by: Sequence[str] = (),
) -> dict[str, Any]:
    if state is not None:
        _require_human_approval(
            human_approved, f"Changing the ticket state to '{state}'"
        )
    add_labels, remove_labels = _cleaned(add_labels), _cleaned(remove_labels)
    add_blocked_by, remove_blocked_by = (
        _cleaned(add_blocked_by),
        _cleaned(remove_blocked_by),
    )
    if (
        body is None
        and title is None
        and state is None
        and parent is None
        and not (add_labels or remove_labels or add_blocked_by or remove_blocked_by)
    ):
        raise MemberCapabilityError(
            "ticket update needs at least one of body, title, labels, state, "
            "parent, or blockers."
        )
    updated = await _tickets(context).update_ticket(
        url,
        body=body,
        title=title,
        add_labels=add_labels,
        remove_labels=remove_labels,
        state=state,
        state_reason=state_reason,
        parent=parent,
        add_blocked_by=add_blocked_by,
        remove_blocked_by=remove_blocked_by,
    )
    return updated.model_dump()


async def ticket_reaction(
    context: Context, url: str, target_id: str, reaction: SemanticReaction
) -> dict[str, Any]:
    added = await _tickets(context).add_reaction(url, target_id, reaction)
    return added.model_dump()


async def pr_create(
    context: Context,
    repository: str,
    head: str,
    base: str,
    title: str,
    body: str,
    ticket_urls: Sequence[str],
    draft: str,
    closes: bool = False,
) -> dict[str, Any]:
    created = await _code(context).create_pull_request(
        repository,
        head,
        base,
        title,
        body,
        draft=draft == "true",
        tickets=[TicketLink(url, closes) for url in ticket_urls],
    )
    return created.model_dump()


async def pr_update(
    context: Context,
    url: str,
    body: str | None = None,
    title: str | None = None,
    *,
    tickets: Sequence[TicketLink] | None = None,
) -> dict[str, Any]:
    if body is None and title is None and tickets is None:
        raise MemberCapabilityError("pr update needs a body, a title, or ticket links.")
    updated = await _code(context).update_pull_request(
        url, body=body, title=title, tickets=tickets
    )
    return updated.model_dump()


async def pr_comment(context: Context, url: str, body: str) -> dict[str, Any]:
    written = await _code(context).comment(url, body)
    return written.model_dump()


async def pr_review(
    context: Context, url: str, body: str, verdict: ReviewVerdict, head: str
) -> dict[str, Any]:
    if verdict not in get_args(ReviewVerdict):
        raise MemberCapabilityError(
            "Review verdict must be one of " + ", ".join(get_args(ReviewVerdict)) + "."
        )
    return (await _code(context).review(url, body, verdict, head)).model_dump()


async def pr_review_comment(
    context: Context,
    url: str,
    body: str,
    head: str,
    path: str,
    line: int,
    side: str,
    start_line: int | None,
    start_side: str | None,
) -> dict[str, Any]:
    _validate_review_comment_location(path, line, side, start_line, start_side)
    written = await _code(context).review_comment(
        url, body, head, path, line, side, start_line, start_side
    )
    return written.model_dump()


async def pr_reply(
    context: Context, url: str, reply_to: str, body: str
) -> dict[str, Any]:
    written = await _code(context).reply(url, reply_to, body)
    return written.model_dump()


async def pr_reaction(
    context: Context, url: str, target_id: str, reaction: SemanticReaction
) -> dict[str, Any]:
    added = await _code(context).add_reaction(url, target_id, reaction)
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
    code: CodeHostingService, repository: str, branch: str
) -> list[dict[str, Any]]:
    """Readiness of the open pull requests whose head is the pushed branch."""
    results = []
    for url in await code.open_pull_requests(repository, branch):
        checks = await code.readiness(url)
        if checks.readiness == "not_applicable":
            continue
        results.append(
            {
                "url": checks.target.url,
                "readiness": checks.readiness,
                "completion_blockers": [
                    blocker.model_dump() for blocker in checks.completion_blockers
                ],
            }
        )
    return results


async def task_completion_readiness(
    context: Context, work: Work, evidence: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Revalidate every PR a run doing ``work`` is answerable for before
    completion.

    Raises:
        MemberCapabilityError: If a target is not ready.
    """
    results = [result for _, result in await _answered(context, work, evidence)]
    blocked = [result for result in results if result.readiness != "ready"]
    if blocked:
        raise MemberCapabilityError(
            "Task cannot be completed because PR readiness is blocked. "
            + _blockers(blocked)
        )
    return [result.model_dump() for result in results]


async def await_checks(
    context: Context, work: Work, run_id: str, store: RunStore
) -> list[dict[str, Any]]:
    """End a run doing ``work`` to await its PRs' checks, saying so on each PR
    whose checks are pending and recording that the run awaits them; the
    patrol resumes ``work`` for each once they settle.

    Raises:
        MemberCapabilityError: Unless pending checks are all that blocks the
            completion of the PRs the run answers for, none of them a draft:
            the patrol resumes nobody for a draft.
        ChatUpdatesRequired: When the run answers a chat whose new input it
            has not read; nothing is said or recorded then.
    """
    answered = await _answered(context, work, store.evidence(run_id))
    awaited = [(pr, result) for pr, result in answered if result.readiness != "ready"]
    if not awaited or any(
        pr["draft"] or blocker.code != "checks_pending"
        for pr, result in awaited
        for blocker in result.completion_blockers
    ):
        raise MemberCapabilityError(
            "A run awaits checks only when pending checks are all that blocks "
            "its own PRs' completion, none of them a draft. "
            + (
                _blockers([result for _, result in awaited])
                or "No PR of the member's own is blocked."
            )
        )
    code = _code(context)
    for _, result in awaited:
        await pull_request_patrol.await_checks(
            code,
            result.target.url,
            person_id=context.person.person_id,
            run_id=run_id,
            work=work,
            subject=work_subject(work)[1],
            head=result.head_sha,
        )
        store.append_evidence(
            run_id,
            RunStore.CHECKS_AWAITED_EVIDENCE,
            {"target": result.target.model_dump()},
        )
    return [result.model_dump() for _, result in answered]


async def _answered(
    context: Context, work: Work, evidence: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], Readiness]]:
    """Every open PR a run doing ``work`` is answerable for, with its
    readiness: for a ticket's work, the ticket itself when it is a PR, else
    the PRs that refer to it; and for any work, the PRs the run wrote to,
    found open, or was resumed for (evidence). Each counts only when the
    member authored it. A reviewer cannot make someone else's PR ready, and
    the patrol resumes the member only for its own PRs, so no other PR's
    checks are the run's to await.
    """
    urls = list(_pull_request_urls(evidence))
    if work.kind != "ticket" and not urls:
        return []
    code = context.get_code_hosting_service()
    if work.kind == "ticket":
        urls = [*await _ticket_pull_requests(code, work.identity), *urls]
    answered = []
    for url in dict.fromkeys(urls):
        pr = (await code.read("pull_requests", url)).items[0]
        if pr["state"] != "open" or pr["author_type"] != Message.ASSISTANT:
            continue
        result = await code.readiness(url)
        if result.readiness != "not_applicable":
            answered.append((pr, result))
    return answered


async def _ticket_pull_requests(code: CodeHostingService, ticket_url: str) -> list[str]:
    """The ticket itself when it is a pull request, else the pull requests
    that refer to it."""
    try:
        return [(await code.read("pull_requests", ticket_url)).items[0]["url"]]
    except ServiceNotFound:
        return await _referring_pull_requests(code, ticket_url)


def _blockers(results: list[Readiness]) -> str:
    return "; ".join(
        f"{result.target.url}: "
        + ", ".join(blocker.message for blocker in result.completion_blockers)
        for result in results
    )


async def _referring_pull_requests(
    code: CodeHostingService, ticket_url: str
) -> list[str]:
    """The code host's pull requests that refer to the ticket; none when the
    ticket is another service's, whose pull requests the code host cannot
    tell."""
    urls: list[str] = []
    continuation = ""
    while True:
        try:
            page = await code.read(
                "ticket_pull_requests", ticket_url, continuation=continuation
            )
        except ServiceUnsupported:
            return []
        urls.extend(str(item["url"]) for item in page.items)
        if not page.continuation:
            return urls
        continuation = page.continuation


def _pull_request_urls(evidence: list[dict[str, Any]]) -> list[str]:
    """The pull requests a run's evidence names: what it wrote to, what its
    pushes found open, and what it was resumed for."""
    urls: list[str] = []
    for record in evidence:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        target = payload.get("target")
        if isinstance(target, dict) and target.get("kind") == "pull_request":
            urls.append(str(target.get("url") or ""))
        pull_requests = payload.get("pull_requests")
        if isinstance(pull_requests, list):
            urls.extend(
                str(item.get("url") or "")
                for item in pull_requests
                if isinstance(item, dict)
            )
    return [url for url in dict.fromkeys(urls) if url]


def _code(context: Context) -> CodeHostingService:
    """The code host, once the chat the run answers is current: a write made
    without reading what was said since would answer an old conversation."""
    ensure_chat_current(context.person.person_id)
    return context.get_code_hosting_service()


def _tickets(context: Context) -> TicketService:
    """The ticket service, once the chat the run answers is current."""
    ensure_chat_current(context.person.person_id)
    return context.get_ticket_service()


def _cleaned(labels: Sequence[str]) -> list[str]:
    return [name for name in (label.strip() for label in labels) if name]


def _require_human_approval(human_approved: bool, action: str) -> None:
    """Guard the writes that stay a human decision.

    Opening and closing tickets shape the team's backlog, so the member may only
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
