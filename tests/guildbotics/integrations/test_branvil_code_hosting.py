"""The Branvil code host beyond what the port's contract asks: the pull
request patrol, readiness and its failed logs, the addresses it knows, the
member's git credential, how Branvil's answers become the ports' failures,
and how a Branvil that is restarting is waited for."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from types import SimpleNamespace

import httpx
import pytest

from guildbotics.commands.python_command import _load_python_module
from guildbotics.entities.task import Task
from guildbotics.integrations.branvil import api
from guildbotics.runtime.code_hosting_service import TicketLink
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    member_invocation_scope,
)
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    ServiceUnsupported,
    ServiceUnavailable,
    WorkflowStatus,
)
from guildbotics.runtime.resources import MAX_PAGE_BYTES
from guildbotics.utils.fileio import get_template_path
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.branvil_double import (
    BASE,
    MEMBER,
    REPO,
    SCOPE,
    THEIR_REPO,
    BranvilDouble,
    refuse,
    serve,
    services,
    wait_virtually,
)

PULL = f"{BASE}/{REPO}/-/pulls/1"


async def _pull(
    double: BranvilDouble, code, author: str = MEMBER, tickets: Sequence[str] = ()
) -> str:
    double.branch("feature")
    created = await code.create_pull_request(
        REPO,
        "feature",
        "",
        "Fix",
        "",
        draft=False,
        tickets=[TicketLink(url, closes=False) for url in tickets],
    )
    double.pulls[created.target.url]["author"] = author
    return created.target.url


async def _work(code) -> list[str | None]:
    return [task.trigger_reason for task in await code.pull_request_candidates()]


# -- the patrol -----------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["author", "reviewer"])
async def test_waiting_eligibility_obeys_interactive_author_exclusion(
    monkeypatch, role
):
    double, code, _ = services(monkeypatch)
    ticket = double._ticket(REPO, "Work", "", [])["url"]
    double.tickets[ticket]["interactive"] = True
    url = await _pull(double, code, tickets=[ticket])
    _role(
        double,
        url,
        ticket,
        "pull_request_feedback" if role == "author" else "pull_request_review",
    )
    assert await code.can_receive_work(url) is (role == "reviewer")
    assert bool(await code.pull_request_candidates()) is (role == "reviewer")
    assert not double.pulls[url]["draft"]


@pytest.mark.asyncio
async def test_waiting_eligibility_refuses_review_limit_without_drafting(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    _past_the_review_limit(double, url)
    assert not await code.can_receive_work(url)
    assert not double.pulls[url]["draft"]


@pytest.mark.asyncio
async def test_the_author_answers_what_others_said(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    assert await _work(code) == []

    double.comment(url, "otota", "Please rename it.")

    [task] = await code.pull_request_candidates()
    assert (task.url, task.pull_request_url, task.status) == (
        url,
        url,
        Task.IN_PROGRESS,
    )
    assert (task.label, task.trigger_reason) == ("!1", "pull_request_feedback")
    assert await code.refresh_pull_request(task) == task
    # A reaction answers it too.
    comment = double.entries[url]["comments"][-1]
    comment["reactions"].append({"author": "Aiko", "reaction": "ack"})
    assert await code.refresh_pull_request(task) is None


def _role(double: BranvilDouble, url: str, ticket: str, role: str) -> None:
    """Have the pull request ask ``role`` of the member."""
    if role == "pull_request_review":
        double.make_theirs(url)
        double.pulls[url]["reviewers"] = ["aiko"]
    elif role == "pull_request_feedback":
        double.comment(url, "otota", "Please rename it.")
    else:
        awaiting = WorkflowStatus(
            reason="awaiting_checks",
            person_id=MEMBER,
            run_id="run-1",
            subject=ticket,
            head="feature-sha",
            work_kind="ticket",
            work_identity=ticket,
        )
        double.comment(url, MEMBER, "Waiting for CI.", **awaiting.payload())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role", ["pull_request_feedback", "pull_request_checks", "pull_request_review"]
)
@pytest.mark.parametrize("depth", [0, 2], ids=["itself", "grandparent"])
async def test_an_interactive_ticket_leaves_its_authors_pull_request_to_the_human(
    monkeypatch, role, depth
):
    double, code, _ = services(monkeypatch)
    ticket = double._ticket(REPO, "Fix", "", [])["url"]
    holder = double.ancestor(ticket, depth)
    url = await _pull(double, code, tickets=[ticket])
    _role(double, url, ticket, role)
    [task] = await code.pull_request_candidates()
    assert task.trigger_reason == role
    read: list[str] = []
    respond = double.respond

    def recorded(request: httpx.Request) -> httpx.Response:
        read.append(request.url.path)
        return respond(request)

    monkeypatch.setattr(double, "respond", recorded)

    double.tickets[holder]["interactive"] = True
    # Reviewing it stays the member's, without reading the ticket.
    reviewed = role == "pull_request_review"
    assert await _work(code) == ([role] if reviewed else [])
    assert await code.refresh_pull_request(task) == (task if reviewed else None)
    assert (f"/api/v1/repos/{REPO}/tickets/1" in read) != reviewed

    double.tickets[holder]["interactive"] = False
    assert await _work(code) == [role]
    assert await code.refresh_pull_request(task) == task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("links", "interactive"),
    [
        (["https://tickets.example.com/T-1"], False),
        (["quiet"], False),
        (["https://tickets.example.com/T-1", "quiet", "interactive"], True),
    ],
    ids=["another_service", "not_interactive", "one_of_many"],
)
async def test_only_a_linked_interactive_ticket_of_branvil_holds_the_author(
    monkeypatch, links, interactive
):
    double, code, _ = services(monkeypatch)
    quiet, held = (double._ticket(REPO, name, "", [])["url"] for name in "QH")
    double.tickets[held]["interactive"] = True
    named = {"quiet": quiet, "interactive": held}
    url = await _pull(double, code, tickets=[named.get(link, link) for link in links])
    double.comment(url, "otota", "Please rename it.")

    assert await _work(code) == ([] if interactive else ["pull_request_feedback"])


@pytest.mark.asyncio
async def test_a_requested_review_is_consumed_by_the_review(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    double.pulls[url]["reviewers"] = ["Aiko"]
    assert await _work(code) == ["pull_request_review"]

    await code.review(url, "Looks good.", "approve", "feature-sha")
    assert double.pulls[url]["reviewers"] == []
    assert await _work(code) == []

    double.advance("feature")
    assert await _work(code) == ["pull_request_review"]


@pytest.mark.asyncio
async def test_a_requested_review_is_not_held_by_the_review_limit(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    _past_the_review_limit(double, url)
    double.pulls[url]["reviewers"] = ["aiko"]
    before = len(double.writes)

    assert await _work(code) == ["pull_request_review"]
    assert double.writes[before:] == []


@pytest.mark.asyncio
async def test_a_pull_request_outside_the_scope_is_not_patrolled(monkeypatch):
    double, code, _ = services(monkeypatch)
    double.branch("theirs")
    double._new_pull(
        THEIR_REPO,
        {"head": "theirs", "base": "", "title": "T", "body": "", "draft": False}
        | {"tickets": []},
    )
    double.comment(f"{BASE}/{THEIR_REPO}/-/pulls/1", "otota", "Hi")

    assert await code.pull_request_candidates() == []


@pytest.mark.asyncio
async def test_the_members_failure_holds_the_pull_request_until_others_act(
    monkeypatch,
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.comment(url, "otota", "Please fix.")
    failed = WorkflowStatus(reason="failed", person_id=MEMBER, run_id="r")
    await code.comment(url, "It failed.", status=failed)
    assert await _work(code) == []

    double.comment(url, "otota", "Try again.")
    assert await _work(code) == ["pull_request_feedback"]


@pytest.mark.asyncio
async def test_marking_the_pull_request_ready_lifts_the_members_hold(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.comment(url, "otota", "Please fix.")
    failed = WorkflowStatus(reason="failed", person_id=MEMBER, run_id="r")
    await code.comment(url, "It failed.", status=failed)
    assert await _work(code) == []

    double.ready(url)

    assert await _work(code) == ["pull_request_feedback"]


@pytest.mark.asyncio
async def test_a_thread_reply_answers_what_the_review_says(monkeypatch):
    """A reply in the thread a review points to is the member's word on the
    pull request, as GitHub records it as a review."""
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    root = double.inline(url, "ken", "Rename this.")
    double.review(url, "ken", "See inline comments", "request_changes")
    assert await _work(code) == ["pull_request_feedback"]

    await code.reply(url, root["id"], "Renamed.")

    assert await _work(code) == []


@pytest.mark.asyncio
async def test_a_reviewer_who_spoke_only_in_threads_answers_what_follows(
    monkeypatch,
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="ken")
    root = double.inline(url, "ken", "Is this right?")
    await code.reply(url, root["id"], "Yes.")
    # Involved by its reply alone, the member has answered the thread.
    assert await _work(code) == []

    double.comment(url, "ken", "Then what about the tests?")

    assert await _work(code) == ["pull_request_review"]


@pytest.mark.asyncio
async def test_a_reviewers_inline_comment_reviews_the_head_it_is_on(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="ken")
    double.pulls[url]["reviewers"] = ["aiko"]
    await code.review_comment(
        url, "Here.", "feature-sha", "a.py", 1, "RIGHT", None, None
    )
    # The inline comment answered the review request.
    assert await _work(code) == []

    double.advance("feature")

    assert await _work(code) == ["pull_request_review"]


@pytest.mark.asyncio
async def test_an_inline_comment_lands_only_on_a_file_the_pull_request_changes(
    monkeypatch,
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    before = len(double.writes)

    with pytest.raises(ServiceError, match="not changed"):
        await code.review_comment(
            url, "Here.", "feature-sha", "b.py", 1, "RIGHT", None, None
        )

    assert double.writes[before:] == []


@pytest.mark.asyncio
async def test_the_authors_own_review_is_only_a_comment(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    before = len(double.writes)

    for verdict in ("approve", "request_changes"):
        with pytest.raises(ServiceError, match="only comment"):
            await code.review(url, "Looks good.", verdict, "feature-sha")

    assert double.writes[before:] == []
    submitted = await code.review(url, "Note.", "comment", "feature-sha")
    assert submitted.verdict == "comment"


@pytest.mark.asyncio
async def test_a_resolved_thread_waits_on_no_one(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    root = double.inline(url, "ken", "Rename this.")
    assert await _work(code) == ["pull_request_feedback"]

    double.resolved.add(root["id"])

    assert await _work(code) == []


@pytest.mark.asyncio
async def test_a_pull_request_whose_head_is_gone_is_not_worked_on(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.comment(url, "ken", "Please fix.")
    assert await _work(code) == ["pull_request_feedback"]

    double.branches.pop("feature")

    assert await _work(code) == []


@pytest.mark.asyncio
async def test_a_body_change_keeps_the_ticket_links_unless_they_are_dropped(
    monkeypatch,
):
    double, code, tickets = services(monkeypatch)
    ticket = (await tickets.create_ticket(REPO, "Fix", "", [])).target.url
    double.branch("feature")
    url = (
        await code.create_pull_request(
            REPO,
            "feature",
            "",
            "Fix",
            "",
            draft=False,
            tickets=[TicketLink(ticket, closes=True)],
        )
    ).target.url

    async def linked() -> list[str]:
        page = await code.read("ticket_pull_requests", ticket)
        return [pull["url"] for pull in page.items]

    await code.update_pull_request(url, body="New", title=None, tickets=None)
    assert await linked() == [url]

    await code.update_pull_request(url, body="Newer", title=None, tickets=[])
    assert await linked() == []
    assert double.pulls[url]["body"] == "Newer"


@pytest.mark.asyncio
async def test_unsupported_closing_links_never_reach_a_patch(monkeypatch):
    double, code, tickets = services(monkeypatch)
    own = (await tickets.create_ticket(REPO, "Own", "", [])).target.url
    other = double.their_ticket
    double.branch("feature")
    url = (
        await code.create_pull_request(
            REPO, "feature", "", "Title", "Body", draft=False, tickets=[]
        )
    ).target.url
    call = code.call
    attempts = []

    async def checked_call(method, path, data):
        attempts.append(method)
        return await call(method, path, data)

    monkeypatch.setattr(code, "call", checked_call)
    for foreign in (other, "https://tickets.example.com/T-1"):
        with pytest.raises(ServiceUnsupported):
            await code.update_pull_request(
                url,
                body="Changed",
                title="Changed",
                tickets=[TicketLink(own, True), TicketLink(foreign, True)],
            )
    assert attempts == []


def _past_the_review_limit(double: BranvilDouble, url: str) -> None:
    for _ in range(3):
        double.review(url, MEMBER)
        double.advance("feature")


@pytest.mark.asyncio
async def test_review_limit_is_a_read_only_decision_for_the_selector(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    _past_the_review_limit(double, url)
    before = len(double.writes)
    assert await _work(code) == ["pull_request_review_limit"]
    [task] = await code.pull_request_candidates()
    refreshed = await code.refresh_pull_request(task)
    assert refreshed is not None and refreshed.trigger_reason == task.trigger_reason
    assert double.writes[before:] == []
    assert not double.pulls[url]["draft"]
    assert double.entries[url]["comments"] == []


@pytest.mark.asyncio
async def test_a_refused_draft_conversion_is_a_port_failure(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    double.refuse_draft = True
    with pytest.raises(ServiceError):
        await code.convert_pull_request_to_draft(url)
    assert not double.pulls[url]["draft"]
    assert double.entries[url]["comments"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_to_draft", [True, False])
async def test_draft_conversion_rechecks_a_revision_lost_to_another_writer(
    monkeypatch, changed_to_draft
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code, author="otota")
    call = code.call
    revisions = []

    async def racing(method, path, body=None, **kwargs):
        if method == "PATCH":
            revisions.append(body["revision"])
            if len(revisions) == 1:
                # The human or another reviewer wins after this caller read.
                double.pulls[url]["draft"] = changed_to_draft
                double.pulls[url]["revision"] += 1
        return await call(method, path, body, **kwargs)

    monkeypatch.setattr(code, "call", racing)
    assert await code.convert_pull_request_to_draft(url) is (not changed_to_draft)
    assert revisions == ([1] if changed_to_draft else [1, 2])
    assert double.pulls[url]["draft"]
    assert double.entries[url]["comments"] == []


# -- readiness ------------------------------------------------------------


def _check(name: str, conclusion: str | None, status: str = "completed", **more):
    return {"name": name, "status": status, "conclusion": conclusion, **more}


@pytest.mark.asyncio
async def test_failed_checks_block_and_bring_their_primary_logs(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.checks["feature-sha"] = [
        _check("test", "failure", attempt_id="att_9", artifact_names=["report"]),
        _check("lint", "cancelled", attempt_id="att_8"),
        _check("build", "success", attempt_id="att_7"),
    ]
    double.logs.update(att_9="boom", att_8="stopped")

    readiness = await code.readiness(url, failed_logs=True)

    assert (readiness.rollup, readiness.readiness) == ("failure", "blocked")
    assert [b.code for b in readiness.completion_blockers] == ["checks_failed"]
    [log] = readiness.failed_logs or []
    assert (log.job_id, log.name, log.log, log.artifact_names) == (
        "att_9",
        "test",
        "boom",
        ["report"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checks", "expected", "behind_by", "codes", "message"),
    [
        (
            [_check("test", None, "queued")],
            False,
            0,
            ["checks_pending"],
            "still pending",
        ),
        ([], True, 0, ["checks_pending"], "not been registered"),
    ],
    ids=["pending", "not_registered"],
)
async def test_readiness_waits_for_ci(
    monkeypatch, checks, expected, behind_by, codes, message
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.checks["feature-sha"] = checks
    double.checks_expected, double.behind_by = expected, behind_by

    readiness = await code.readiness(url)

    assert [b.code for b in readiness.completion_blockers] == codes
    assert message in readiness.completion_blockers[0].message
    assert readiness.checks_expected is expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "conclusion", "status", "expected"),
    [
        ("gate/test", "failure", "completed", "checks_failed"),
        ("gate/test", None, "running", "checks_pending"),
        ("gate", "failure", "completed", "checks_failed"),
    ],
)
async def test_gate_checks_block_without_requiring_base_integration(
    monkeypatch, name, conclusion, status, expected
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    details = "Conflicts: src/one.py, src/two.py"
    double.behind_by = 2
    double.checks["feature-sha"] = [
        {**_check(name, conclusion, status), "details": details, "attempt_id": None}
    ]

    page = await code.read(
        "pull_request_readiness", url, parameters={"failed_logs": True}
    )

    [result] = page.items
    assert (result["behind_by"], result["out_of_date"], result["readiness"]) == (
        2,
        True,
        "blocked",
    )
    assert [b["code"] for b in result["completion_blockers"]] == [expected]
    assert result["checks"][0]["details"] == details
    assert result["failed_logs"] == []


@pytest.mark.asyncio
async def test_a_head_that_moves_while_it_is_checked_is_not_ready(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    respond = double.respond

    def moving(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        if request.url.path.endswith("/checks"):
            double.advance("feature")
        return response

    double.respond = moving  # type: ignore[method-assign]
    _, code, _ = services(monkeypatch, double)

    readiness = await code.readiness(url)

    assert [b.code for b in readiness.completion_blockers] == ["head_changed"]
    assert readiness.current_head_sha != readiness.head_sha


@pytest.mark.asyncio
async def test_a_closed_pull_request_is_not_judged(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.pulls[url]["state"] = "closed"
    double.checks_expected = True

    readiness = await code.readiness(url)

    assert (readiness.readiness, readiness.base_sha, readiness.checks_expected) == (
        "not_applicable",
        None,
        False,
    )


@pytest.mark.asyncio
async def test_failed_logs_are_fitted_to_one_page(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    double.checks["feature-sha"] = [
        _check(f"job{n}", "failure", attempt_id=f"att_{n}") for n in range(10)
    ]
    double.logs.update({f"att_{n}": "x" * MAX_PAGE_BYTES for n in range(10)})

    page = await code.read(
        "pull_request_readiness", url, parameters={"failed_logs": True}
    )

    assert len(page.model_dump_json().encode()) <= MAX_PAGE_BYTES
    logs = page.items[0]["failed_logs"]
    assert len(logs) == 10 and all(log["truncated"] for log in logs)
    assert all(log["tail_limit_bytes"] == log["log_bytes"] for log in logs)


# -- reads ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_files_patch_names_the_lines_a_comment_can_be_put_on(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    respond = double.respond
    patch = "@@ -1,2 +1,2 @@\n keep\n-old\n+new"

    def files(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            changed = {"path": "a.py", "previous_path": None, "status": "modified"}
            changed |= {"additions": 1, "deletions": 1, "patch": patch}
            return httpx.Response(200, json={"items": [changed], "next_cursor": "o:1"})
        return respond(request)

    double.respond = files  # type: ignore[method-assign]
    _, code, _ = services(monkeypatch, double)

    page = await code.read("pull_request_files", url)

    [changed] = page.items
    assert page.continuation == "o:1"
    assert (changed["patch_complete"], changed["changes"]) == (True, 2)
    assert [(line["line"], line["side"]) for line in changed["commentable_lines"]] == [
        (1, "RIGHT"),
        (2, "LEFT"),
        (2, "RIGHT"),
    ]


@pytest.mark.asyncio
async def test_a_cursor_branvil_did_not_give_is_refused(monkeypatch):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)

    with pytest.raises(ServiceError, match=t("integrations.resources.continuation")):
        await code.read("pull_request_comments", url, continuation="forged")


# -- dependency alerts ------------------------------------------------------


async def _alerts(code, continuation: str = "", **parameters):
    return await code.read(
        "dependency_alerts", REPO, parameters=parameters, continuation=continuation
    )


@pytest.mark.asyncio
async def test_the_upstreams_alerts_are_read_by_state_page_by_page(monkeypatch):
    double, code, _ = services(monkeypatch)
    opened = [double.alert(), double.alert(), double.alert(patched=None)]
    resolved, dismissed = double.alert("resolved"), double.alert("dismissed")

    first = await _alerts(code, state="open", page_size=2)
    rest = await _alerts(code, first.continuation or "", state="open", page_size=2)

    assert [a["id"] for a in first.items + rest.items] == opened
    assert first.continuation and rest.continuation is None
    assert rest.items[0]["patched_version"] is None
    assert first.items[0] == double.alerts[0]
    for state, alert in (("resolved", resolved), ("dismissed", dismissed)):
        page = await _alerts(code, state=state)
        assert [a["id"] for a in page.items] == [alert]


@pytest.mark.asyncio
async def test_one_alert_is_read_by_its_id(monkeypatch):
    double, code, _ = services(monkeypatch)
    double.alert()
    second = double.alert("dismissed")

    page = await _alerts(code, id=second)

    assert page.items == [double.alerts[1]] and page.continuation is None
    with pytest.raises(ServiceNotFound):
        await _alerts(code, id="99")


@pytest.mark.asyncio
async def test_no_alert_is_an_empty_page_and_no_upstream_none_at_all(monkeypatch):
    double, code, _ = services(monkeypatch)

    page = await _alerts(code)

    assert (page.items, page.continuation) == ([], None)
    double.upstreams.clear()
    with pytest.raises(ServiceUnsupported):
        await _alerts(code)


@pytest.mark.asyncio
async def test_the_alert_command_shows_the_upstreams_alerts(monkeypatch):
    double, code, _ = services(monkeypatch)
    double.alert(patched=None)
    module = _load_python_module(
        get_template_path() / "commands/repository/security_alerts.py"
    )
    context = SimpleNamespace(
        person=SimpleNamespace(person_id=MEMBER),
        get_code_hosting_service=lambda: code,
    )

    shown = json.loads(await module.main(context, REPO, output="json"))

    assert shown == {"repo": REPO, "alerts": double.alerts, "continuation": None}


# -- addresses ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", [BASE, f"{BASE}/"])
async def test_an_address_is_this_branvils_by_its_base_url(monkeypatch, base_url):
    _, code, _ = services(monkeypatch, code={"base_url": base_url})
    remote = await code.clone_url(REPO)

    assert remote == f"{BASE}/git/{REPO}.git"
    assert code.repository_from_remote(remote) == REPO
    assert code.repository_from_remote(remote.removesuffix(".git")) == REPO
    assert code.commit_url(REPO, "abc") == f"{BASE}/{REPO}/-/commits/abc"
    for other in (
        f"http://127.0.0.1:7879/git/{REPO}.git",
        f"http://aiko@127.0.0.1:7878/git/{REPO}.git",
        f"{BASE}/{REPO}.git",
        f"{BASE}/git/demo.git",
    ):
        assert code.repository_from_remote(other) is None, other


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPO}/pull/1",
        f"http://127.0.0.1:7879/{REPO}/-/pulls/1",
        f"{BASE}/{REPO}/-/tickets/1",
        f"{BASE}/{REPO}/-/pulls/0",
        f"{BASE}/{REPO}/-/pulls/1?x=1",
        f"{BASE}/demo/-/pulls/1",
    ],
)
async def test_an_address_of_no_pull_request_is_not_found(monkeypatch, url):
    double, code, _ = services(monkeypatch)
    before = len(double.writes)

    with pytest.raises(ServiceNotFound):
        await code.comment(url, "Hi")

    assert double.writes[before:] == []


# -- the member -----------------------------------------------------------


@pytest.mark.asyncio
async def test_git_authenticates_with_the_members_name_and_token(monkeypatch):
    _, code, _ = services(monkeypatch)

    credential = await code.git_credential()

    assert (credential.username, credential.password) == (MEMBER, "bvt_aiko")
    assert "bvt_aiko" not in repr(credential)
    assert await code.identity(check=True) == {"branvil_username": MEMBER}


@pytest.mark.asyncio
async def test_without_a_token_nothing_reaches_branvil(monkeypatch):
    double, code, _ = services(monkeypatch)
    monkeypatch.delenv("AIKO_BRANVIL_TOKEN")
    monkeypatch.setattr(
        double, "respond", lambda request: pytest.fail(f"{request.url} was asked")
    )

    with pytest.raises(ServiceError, match="AIKO_BRANVIL_TOKEN"):
        await code.git_credential()
    with pytest.raises(ServiceError, match="AIKO_BRANVIL_TOKEN"):
        await code.default_branch(REPO)


@pytest.mark.asyncio
async def test_a_token_of_another_member_is_refused_by_the_identity_check(
    monkeypatch,
):
    recorded: list[dict] = []
    monkeypatch.setattr(
        api, "record_correlated_event", lambda **kwargs: recorded.append(kwargs)
    )
    double = BranvilDouble()
    respond = double.respond
    double.respond = lambda request: (  # type: ignore[method-assign]
        httpx.Response(200, json={"name": "otota"})
        if request.url.path.endswith("/me")
        else respond(request)
    )
    _, code, _ = services(monkeypatch, double)

    with pytest.raises(ServiceError, match="otota"):
        await code.identity(check=True)

    [event] = recorded
    assert (event["event_type"], event["payload"]["code"]) == (
        "credential.failed",
        "identity_mismatch",
    )


@pytest.mark.asyncio
async def test_the_identity_check_asks_for_a_missing_username(monkeypatch):
    double, code, _ = services(monkeypatch)
    code.person.account_info.clear()
    before = len(double.writes)

    with pytest.raises(ServiceError) as raised:
        await code.identity(check=True)

    assert str(raised.value) == t("integrations.branvil.api.username_missing")
    assert double.writes[before:] == []


# -- failures -------------------------------------------------------------


def _answering(monkeypatch, respond):
    double = BranvilDouble()
    double.respond = respond  # type: ignore[method-assign]
    _, code, _ = services(monkeypatch, double)
    return code


def _error(status: int, code: str, message: str = "It says no.") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "kind", "message"),
    [
        (_error(404, "not_found"), ServiceNotFound, "It says no."),
        (_error(422, "unsupported"), ServiceUnsupported, "It says no."),
        (_error(409, "head_moved"), ServiceError, "head"),
        (_error(400, "label_undefined"), ServiceError, "not defined"),
        (
            _error(409, "conflict"),
            api.Conflict,
            "Branvil refused GET /api/v1/repos/acme/demo",
        ),
        (_error(503, "unavailable"), ServiceError, "503 unavailable"),
        (httpx.Response(200, text="<html>"), ServiceError, "invalid response"),
    ],
)
async def test_branvils_answers_are_the_ports_failures(
    monkeypatch, answer, kind, message
):
    code = _answering(monkeypatch, lambda request: answer)

    with pytest.raises(kind) as raised:
        await code.default_branch(REPO)

    assert type(raised.value) is kind
    assert message in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("parameters", [{}, {"id": "1"}])
@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (_error(409, "no_account"), ServiceError),
        (_error(409, "upstream_refused"), ServiceError),
        (_error(503, "unavailable"), ServiceError),
        (_error(404, "not_found"), ServiceNotFound),
        (_error(422, "unsupported"), ServiceUnsupported),
    ],
)
async def test_a_failed_read_of_alerts_is_a_failure_with_branvils_words(
    monkeypatch, parameters, answer, kind
):
    code = _answering(monkeypatch, lambda request: answer)

    with pytest.raises(kind) as raised:
        await _alerts(code, **parameters)

    assert type(raised.value) is kind
    assert "It says no." in str(raised.value)


@pytest.mark.asyncio
async def test_a_refused_token_is_recorded_as_the_members(monkeypatch):
    recorded: list[dict] = []
    monkeypatch.setattr(
        api, "record_correlated_event", lambda **kwargs: recorded.append(kwargs)
    )
    code = _answering(monkeypatch, lambda request: _error(401, "unauthorized"))

    with pytest.raises(ServiceError, match="token"):
        await code.default_branch(REPO)

    [event] = recorded
    assert (event["event_type"], event["person_id"]) == ("credential.failed", MEMBER)
    assert event["attributes"]["credential.provider"] == "branvil"


# -- a Branvil that is restarting -----------------------------------------

_INTERVAL = api._RECONNECT_INTERVAL_SECONDS
_BUDGET = 300.0


@pytest.fixture
def caller_budget(monkeypatch):
    # The retry tests use a virtual clock starting at zero.
    wait_virtually(monkeypatch)
    with member_invocation_scope(MemberInvocation(deadline=_BUDGET)):
        yield


async def _health_of(client: httpx.AsyncClient) -> None:
    async with client:
        await client.get("/health")


#: Each way to ask Branvil, by whose it is.
_ASKS = {
    "member": lambda: _health_of(
        api.connect(BASE, token="", scope=SCOPE, person_id=MEMBER)
    ),
    "admin": lambda: _health_of(api.administer(BASE, "")),
    "health": lambda: api.health(BASE),
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ask", "waits"), [("member", True), ("admin", False), ("health", False)]
)
async def test_only_a_members_client_waits_for_branvil(
    monkeypatch, ask, waits, caller_budget
):
    double = BranvilDouble()
    serve(double, monkeypatch)
    clock = refuse(double, monkeypatch)

    with pytest.raises(api.Unreachable):
        await _ASKS[ask]()

    assert clock.now == _BUDGET if waits else clock.sleeps == []


@pytest.mark.asyncio
async def test_a_read_waits_for_a_branvil_that_is_restarting(
    monkeypatch, caller_budget
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch, times=3)

    assert await code.default_branch(REPO) == "main"

    assert clock.sleeps == [_INTERVAL] * 3


@pytest.mark.asyncio
async def test_a_write_waits_for_a_branvil_that_is_restarting_and_lands_once(
    monkeypatch,
    caller_budget,
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    before = len(double.writes)
    clock = refuse(double, monkeypatch, times=3, method="POST")

    await code.comment(url, "Hi")

    assert double.writes[before:] == ["POST /api/v1/repos/acme%2Fdemo/pulls/1/comments"]
    assert [entry["body"] for entry in double.entries[url]["comments"]] == ["Hi"]
    assert clock.sleeps == [_INTERVAL] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "key"),
    [(httpx.ConnectError, "unreachable"), (httpx.ConnectTimeout, "timeout")],
)
async def test_a_branvil_that_stays_out_of_reach_is_said_so_after_the_wait(
    monkeypatch, error, key, caller_budget
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch, error=error)

    with pytest.raises(api.Unreachable) as raised:
        await code.default_branch(REPO)

    assert str(raised.value) == t(f"integrations.branvil.api.{key}", base_url=BASE)
    assert clock.now == _BUDGET


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "key"),
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.RemoteProtocolError, "unreachable"),
        (httpx.ReadError, "unreachable"),
        (httpx.WriteError, "unreachable"),
    ],
)
async def test_a_request_that_may_have_reached_branvil_is_not_sent_again(
    monkeypatch, error, key, caller_budget
):
    double, code, _ = services(monkeypatch)
    url = await _pull(double, code)
    before = len(double.writes)
    clock = wait_virtually(monkeypatch)
    respond = double.respond

    def lost(request: httpx.Request) -> httpx.Response:
        answer = respond(request)
        if request.method == "POST":
            raise error("lost", request=request)
        return answer

    monkeypatch.setattr(double, "respond", lost)

    with pytest.raises(api.Unreachable) as raised:
        await code.comment(url, "Hi")

    assert str(raised.value) == t(f"integrations.branvil.api.{key}", base_url=BASE)
    assert double.writes[before:] == ["POST /api/v1/repos/acme%2Fdemo/pulls/1/comments"]
    assert clock.sleeps == []
    assert not isinstance(raised.value, ServiceUnavailable)


@pytest.mark.asyncio
async def test_a_branvil_that_stays_stopped_is_waited_for_once_until_it_answers(
    monkeypatch,
    caller_budget,
):
    double, code, _ = services(monkeypatch)
    respond = double.respond
    clock = refuse(double, monkeypatch)
    with pytest.raises(api.Unreachable):
        await code.default_branch(REPO)
    waited = len(clock.sleeps)

    # The service's next request, git's included, fails at once.
    with member_invocation_scope(MemberInvocation(deadline=2 * _BUDGET)):
        with pytest.raises(api.Unreachable):
            await code.git_credential()
    assert len(clock.sleeps) == waited

    # Once Branvil answers, a later outage is waited for again.
    monkeypatch.setattr(double, "respond", respond)
    with member_invocation_scope(MemberInvocation(deadline=2 * _BUDGET)):
        assert await code.default_branch(REPO) == "main"
    clock = refuse(double, monkeypatch)
    with pytest.raises(api.Unreachable):
        await code.default_branch(REPO)
    assert clock.now == _BUDGET


@pytest.mark.asyncio
async def test_git_waits_for_a_branvil_that_is_restarting(monkeypatch, caller_budget):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch, times=3)

    credential = await code.git_credential()

    assert (credential.username, credential.password) == (MEMBER, "bvt_aiko")
    assert clock.sleeps == [_INTERVAL] * 3


@pytest.mark.asyncio
async def test_git_is_told_of_a_branvil_that_stays_out_of_reach(
    monkeypatch, caller_budget
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch)

    with pytest.raises(api.Unreachable) as raised:
        await code.git_credential()

    assert str(raised.value) == t("integrations.branvil.api.unreachable", base_url=BASE)
    assert clock.now == _BUDGET


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["default_branch", "git_credential"])
@pytest.mark.parametrize(("deadline", "refusals"), [(300.0, 200), (600.0, 400)])
async def test_recovery_uses_the_invocations_remaining_time(
    monkeypatch, operation, deadline, refusals
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch, times=refusals)
    # The client already exists; time spent before this request is not renewed.
    clock.now = 50.0
    with member_invocation_scope(MemberInvocation(deadline=deadline)):
        result = await getattr(code, operation)(
            *([REPO] if operation == "default_branch" else [])
        )
    assert result
    assert clock.now == 50.0 + refusals


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "deadline"), [(50.0, 0.0), (50.0, 50.0), (50.0, 52.5)]
)
@pytest.mark.parametrize("operation", ["default_branch", "git_credential"])
async def test_wait_ends_at_the_callers_deadline(
    monkeypatch, start, deadline, operation
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch)
    clock.now = start
    with member_invocation_scope(MemberInvocation(deadline=deadline)):
        with pytest.raises(api.Unreachable):
            await getattr(code, operation)(
                *([REPO] if operation == "default_branch" else [])
            )
    assert clock.now == (deadline or start)
    assert clock.sleeps == ([1.0, 1.0, 0.5] if deadline and deadline > start else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["default_branch", "git_credential"])
@pytest.mark.parametrize("recovers", [True, False])
async def test_a_caller_without_a_deadline_has_a_finite_operation_budget(
    monkeypatch, operation, recovers
):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch, times=200 if recovers else float("inf"))
    clock.now = 50.0

    async def request():
        return await getattr(code, operation)(
            *([REPO] if operation == "default_branch" else [])
        )

    if recovers:
        assert await request()
        assert clock.now == 250.0
    else:
        with pytest.raises(api.Unreachable):
            await request()
        assert clock.now == 350.0
        # Even another operation budget does not renew an exhausted client.
        with pytest.raises(api.Unreachable):
            await request()
        assert clock.now == 350.0


@pytest.mark.asyncio
async def test_a_connect_attempt_cannot_outlast_the_callers_budget(monkeypatch):
    client = api.connect(BASE, token="", scope=SCOPE, person_id=MEMBER)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client._transport = httpx.MockTransport(stalled)
    client._mounts = {}
    with member_invocation_scope(MemberInvocation(deadline=time.monotonic() + 0.05)):
        with pytest.raises(api.Unreachable):
            await _health_of(client)
    assert entered.is_set()
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["member", "admin", "health"])
async def test_exhaustion_survives_clients_until_any_client_observes_an_answer(
    monkeypatch, answer
):
    double, code, tickets = services(monkeypatch)
    respond = double.respond
    clock = refuse(double, monkeypatch)
    with pytest.raises(api.Unreachable):
        await code.default_branch(REPO)
    assert clock.now == 300.0

    # This service owns a different client, just as a fresh context does.
    with pytest.raises(api.Unreachable):
        await tickets.candidates()
    assert clock.now == 300.0
    with pytest.raises(api.Unreachable):
        await _ASKS["member"]()
    assert clock.now == 300.0

    monkeypatch.setattr(double, "respond", respond)
    await _ASKS[answer]()
    clock = refuse(double, monkeypatch, times=200)
    assert await code.default_branch(REPO) == "main"
    assert clock.now == 200.0


@pytest.mark.asyncio
async def test_an_exhausted_endpoint_does_not_use_another_endpoints_budget(monkeypatch):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch)
    with pytest.raises(ServiceUnavailable):
        await code.default_branch(REPO)
    assert clock.now == 300.0
    with pytest.raises(ServiceUnavailable):
        await _health_of(
            api.connect(
                "http://127.0.0.1:7879", token="", scope=SCOPE, person_id=MEMBER
            )
        )
    assert clock.now == 600.0


@pytest.mark.asyncio
async def test_a_response_during_the_last_sleep_invalidates_the_old_refusal(
    monkeypatch,
):
    double, code, _ = services(monkeypatch)
    respond = double.respond
    clock = refuse(double, monkeypatch)

    async def sleep_and_observe(seconds):
        await clock.sleep(seconds)
        if clock.now == 300.0:
            monkeypatch.setattr(double, "respond", respond)
            await api.health(BASE)

    monkeypatch.setattr(api.asyncio, "sleep", sleep_and_observe)
    with pytest.raises(ServiceUnavailable):
        await code.default_branch(REPO)

    # The older exhausted request must not overwrite the response's fact.
    clock = refuse(double, monkeypatch, times=200)
    assert await code.default_branch(REPO) == "main"
    assert clock.now == 200.0


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["admin", "health"])
async def test_a_failed_probe_does_not_consume_a_members_wait(monkeypatch, probe):
    double, code, _ = services(monkeypatch)
    clock = refuse(double, monkeypatch)
    with pytest.raises(ServiceUnavailable):
        await _ASKS[probe]()
    assert clock.now == 0.0
    with pytest.raises(ServiceUnavailable):
        await code.default_branch(REPO)
    assert clock.now == 300.0
