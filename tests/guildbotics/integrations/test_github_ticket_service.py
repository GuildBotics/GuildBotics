"""GitHub as a member's tickets: the issues it reads and writes, and the
Projects board it selects work from."""

import json
import logging
from typing import Any
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.integrations.github.repository_scope import (
    ADD_PROJECT_ITEM,
    ADD_REACTION,
    NODE_REPOSITORY,
    UPDATE_PROJECT_ITEM_STATUS,
)
from guildbotics.integrations.github.ticket_service import GitHubTicketService
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
    render_workflow_status_comment,
)
from guildbotics.runtime.port import (
    ServiceError,
    ServiceNotFound,
    WorkflowStatus,
)
from guildbotics.runtime.resources import Ticket, WorkTarget
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.integrations.github_services import (
    API,
    LOGIN,
    OWNER,
    REPO,
    ScriptedGitHub,
    issue_url,
    member,
    pages,
    pull_url,
    rendered_body,
    serve,
    team,
    ticket_service,
)

AGENT_FIELD = GitHubTicketService.FIELD_AGENT
#: What marks the read of the board's fields.
_FIELDS = "fields(first"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_field", [False, True])
async def test_waiting_eligibility_uses_assignee_and_agent_field_without_board_writes(
    monkeypatch, agent_field
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=42,
                status="In Progress",
                assignee=None if agent_field else LOGIN,
                agent="⚙aiko" if agent_field else None,
            )
        ],
        comments={42: [_comment("human", "Please answer.")]},
    )
    assert await service.can_receive_work(issue_url(42))
    assert service.moved == []
    assert [task.url for task in await service.candidates()] == [issue_url(42)]


class _Board(GitHubTicketService):
    """The board read from GitHub, and the moves it is asked for."""

    moved: list[tuple[str, str]]

    async def move(self, task: Task, lane: str) -> bool:
        self.moved.append((str(task.url), lane))
        return True

    async def first_task(self) -> Task | None:
        candidates = await self.candidates()
        return candidates[0] if candidates else None


def _board(
    monkeypatch,
    *,
    items: list[dict[str, Any]],
    comments: dict[int, list[dict[str, Any]]] | None = None,
    timeline: dict[int, list[dict[str, Any]]] | None = None,
    lane_map: dict[str, str] | None = None,
    statuses: list[str] | None = None,
    github_username: str = LOGIN,
) -> tuple[_Board, ScriptedGitHub]:
    # Board column order (left -> right): options before the ready lane or
    # at/after the done lane are ignored, and those between them are working
    # lanes.
    order = statuses if statuses is not None else ["Todo", "In Progress", "Done"]
    github = (
        ScriptedGitHub()
        .on(
            "GET",
            "/search/issues",
            {"items": [], "total_count": 0, "incomplete_results": False},
        )
        .on_graphql(_FIELDS, _status_field(*order))
        .on_graphql(
            "fieldValues",
            {"node": {"items": {"nodes": items, "pageInfo": {"hasNextPage": False}}}},
        )
    )
    for item in items:
        number = item["content"]["number"]
        github.on(
            "GET",
            f"/repos/{REPO}/issues/{number}/comments",
            pages((comments or {}).get(number, [])),
        )
        github.on(
            "GET",
            f"/repos/{REPO}/issues/{number}/timeline",
            pages((timeline or {}).get(number, [])),
        )
    serve(github, monkeypatch)
    person = member(github_username=github_username)
    board = {"lane_map": lane_map} if lane_map is not None else {}
    service = _Board(logging.getLogger("test"), person, team(person, **board))
    service._project_node_id = "PROJECT"
    service.moved = []
    return service, github


def _item(
    *,
    number: int,
    status: str,
    assignee: str | None = LOGIN,
    body: str = "",
    agent: str | None = None,
    agent_updated_at: str | None = None,
    assigned_events: list[dict[str, str]] | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    field: str = AGENT_FIELD,
    open_blockers: int = 0,
) -> dict:
    field_values: list[dict[str, Any]] = [{"field": {"name": "Status"}, "name": status}]
    if agent:
        agent_value: dict[str, Any] = {"field": {"name": field}, "name": agent}
        if agent_updated_at:
            agent_value["updatedAt"] = agent_updated_at
        field_values.append(agent_value)
    return {
        "fieldValues": {"nodes": field_values},
        "content": {
            "id": f"I{number}",
            "number": number,
            "url": issue_url(number),
            "title": f"issue {number}",
            "body": body,
            "bodyHTML": rendered_body(body),
            "createdAt": created_at,
            "assignees": {"nodes": [{"login": assignee}] if assignee else []},
            "timelineItems": {
                "nodes": [
                    {
                        "createdAt": event["created_at"],
                        "assignee": {"login": event["login"]},
                    }
                    for event in assigned_events or []
                ]
            },
            "repository": {"name": "demo", "owner": {"login": OWNER}},
            "issueDependenciesSummary": {"blockedBy": open_blockers},
        },
    }


def _comment(user: str, body: str, created_at: str = "2026-01-01T00:00:00Z"):
    return {
        "user": {"login": user},
        "body": body,
        "body_html": rendered_body(body),
        "created_at": created_at,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,html",
    [
        (
            "`@pytest.fixture` を使ってください",
            "<p><code>@pytest.fixture</code> を使ってください</p>",
        ),
        (
            "`npm i -D @types/node` が必要です",
            "<p><code>npm i -D @types/node</code> が必要です</p>",
        ),
        ("```python\n@someone\n```", "<pre><code>@someone</code></pre>"),
    ],
)
async def test_code_in_a_human_comment_is_not_a_recipient(monkeypatch, body, html):
    human = {**_comment("human", body), "body_html": html}
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment(LOGIN, "Done."), human]},
    )
    [task] = await service.candidates()
    assert task.trigger_reason == "issue_comment"


@pytest.mark.asyncio
async def test_the_second_search_page_contains_an_unanswered_mention(monkeypatch):
    count = 101
    service, github = _board(
        monkeypatch,
        items=[
            _item(number=n, status="Todo", assignee=None) for n in range(1, count + 1)
        ],
        comments={
            n: [
                _comment("human", f"@{LOGIN} Please help."),
                *([_comment(LOGIN, "Answered.")] if n < count else []),
            ]
            for n in range(1, count + 1)
        },
    )

    def result(request):
        page = int(request.url.params.get("page", "1"))
        return {
            "items": [
                {"html_url": issue_url(n)}
                for n in range((page - 1) * 100 + 1, min(page * 100, count) + 1)
            ],
            "total_count": count,
            "incomplete_results": False,
        }

    github.on("GET", "/search/issues", result)
    [task] = await service.candidates()
    assert task.url == issue_url(count)
    search = [
        request for request in github.requests if request.url.path == "/search/issues"
    ]
    assert [request.url.params["page"] for request in search] == ["1", "2", "1", "2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 100, 101, 1000])
async def test_search_reads_every_page_without_an_extra_request_at_the_limit(
    monkeypatch, count
):
    service, github = _board(monkeypatch, items=[])

    def page(request):
        number = int(request.url.params["page"])
        assert (request.url.params["sort"], request.url.params["order"]) == (
            "updated",
            "asc",
        )
        return {
            "items": [
                {"html_url": issue_url(n)}
                for n in range((number - 1) * 100 + 1, min(number * 100, count) + 1)
            ],
            "total_count": count,
            "incomplete_results": False,
        }

    github.on("GET", "/search/issues", page)
    assert len(await service._search_mentions()) == count
    assert len(
        [r for r in github.requests if r.url.path == "/search/issues"]
    ) == 2 * max(1, (count + 99) // 100)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["overflow", "incomplete", "http", "malformed", "later_page"]
)
async def test_mention_discovery_failure_preserves_assigned_and_pr_work(
    monkeypatch, caplog, failure
):
    service, github = _board(monkeypatch, items=[_item(number=1, status="Todo")])
    if failure == "http":
        answer = httpx.Response(503, json={"message": "Unavailable"})
    elif failure == "malformed":
        answer = {"unexpected": "shape"}
    elif failure == "later_page":

        def answer(request):
            if request.url.params["page"] == "1":
                return {
                    "items": [{"html_url": issue_url(n)} for n in range(2, 102)],
                    "total_count": 101,
                    "incomplete_results": False,
                }
            return httpx.Response(503, json={"message": "Unavailable"})
    else:
        answer = {
            "items": [],
            "total_count": 1001 if failure == "overflow" else 1,
            "incomplete_results": failure == "incomplete",
        }
    github.on("GET", "/search/issues", answer)
    pr = Task(
        title="PR feedback",
        description="",
        url=pull_url(2),
        pull_request_url=pull_url(2),
    )
    context = SimpleNamespace(
        team=service.team,
        clone_for=lambda _: context,
        get_code_hosting_service=lambda: SimpleNamespace(
            pull_request_candidates=AsyncMock(return_value=[pr])
        ),
        get_ticket_service=lambda: service,
        aclose=service.aclose,
    )
    candidates = await TicketSelector(context).candidates(service.person)
    assert [task.url for task in candidates] == [pull_url(2), issue_url(1)]
    assert "assigned tickets and PR work remain available" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["body", "comment"])
async def test_native_mentions_in_code_do_not_summon_an_unassigned_member(
    monkeypatch, source
):
    issue = _item(number=1, status="Todo", assignee=None)
    comments = []
    if source == "body":
        issue["content"].update(
            body=f"`@{LOGIN}`", bodyHTML=f"<p><code>@{LOGIN}</code></p>"
        )
    else:
        comments = [
            {
                **_comment("human", f"`@{LOGIN}`"),
                "body_html": f"<pre><code>@{LOGIN}</code></pre>",
            }
        ]
    service, github = _board(monkeypatch, items=[issue], comments={1: comments})
    github.on(
        "GET",
        "/search/issues",
        {
            "items": [{"html_url": issue_url(1)}],
            "total_count": 1,
            "incomplete_results": False,
        },
    )
    assert await service.candidates() == []


@pytest.mark.asyncio
async def test_patrol_reads_comment_html_with_the_original_markdown(monkeypatch):
    service, github = _board(
        monkeypatch,
        items=[_item(number=1, status="Todo")],
        comments={1: [_comment("human", "**Hello**")]},
    )
    [task] = await service.candidates()
    assert task.comments[0].content == "**Hello**"
    [request] = [r for r in github.requests if r.url.path.endswith("/comments")]
    assert request.headers["Accept"] == "application/vnd.github.full+json"


@pytest.mark.asyncio
async def test_off_board_mentions_fetch_html_and_refresh_after_an_answer(monkeypatch):
    service, github = _board(monkeypatch, items=[])
    url = issue_url(1)
    body = f"@{LOGIN} Context?"
    snapshot = {
        "html_url": url,
        "node_id": "I1",
        "title": "Context",
        "body": body,
        "body_html": rendered_body(body),
        "state": "open",
        "assignees": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    github.on(
        "GET",
        "/search/issues",
        {"items": [{"html_url": url}], "total_count": 1, "incomplete_results": False},
    )
    github.on("GET", f"/repos/{REPO}/issues/1", snapshot)
    github.on("GET", f"/repos/{REPO}/issues/1/comments", [])
    [task] = await service.candidates()
    assert (task.url, task.trigger_reason, task.assignee) == (
        url,
        "issue_mention",
        None,
    )
    assert await service.refresh(task) is not None
    github.on("GET", f"/repos/{REPO}/issues/1/comments", [_comment(LOGIN, "Answered.")])
    assert await service.refresh(task) is None
    assert all(
        request.headers["Accept"] == "application/vnd.github.full+json"
        for request in github.requests
        if request.url.path.startswith(f"/repos/{REPO}/issues/")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,other,expected",
    [("⚙aiko", True, "issue_comment"), ("⚙yuki", False, None), ("⚙yuki", True, None)],
)
async def test_token_recipients_survive_html_line_boundaries(
    monkeypatch, token, other, expected
):
    html = (
        f"<p>{token}<br>please"
        + (' <a class="user-mention">@otota</a>' if other else "")
        + "</p>"
    )
    comment = {**_comment("human", f"{token}\nplease"), "body_html": html}
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment(LOGIN, "Answered."), comment]},
    )
    service.team.members.append(
        Person(person_id="yuki", name="Yuki", person_type="agent")
    )
    candidates = await service.candidates()
    assert [task.trigger_reason for task in candidates] == (
        [expected] if expected else []
    )


def _referring(state: str, updated_at: str, *, merged: bool = False) -> dict:
    """A timeline event: a pull request that refers to the issue."""
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "html_url": pull_url(2),
                "state": state,
                "updated_at": updated_at,
                "pull_request": {
                    "merged_at": "2026-01-02T00:00:00Z" if merged else None
                },
            }
        },
    }


def _status_comment(reason: str) -> str:
    return render_workflow_status_comment(
        "status",
        WorkflowStatus.model_validate(
            {"reason": reason, "person_id": "aiko", "run_id": "run-1"}
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 60])
async def test_mention_search_reads_only_matching_unassigned_tickets(
    monkeypatch, count
):
    items = [
        _item(number=number, status="Todo", assignee="someone")
        for number in range(1, count + 1)
    ]
    service, github = _board(
        monkeypatch,
        items=items,
        comments={1: [_comment("human", f"@{LOGIN} Context?")]},
    )
    github.on(
        "GET",
        "/search/issues",
        {
            "items": [{"html_url": issue_url(1)}],
            "incomplete_results": False,
            "total_count": 1,
        },
    )
    [task] = await service.candidates()
    assert (task.url, task.assignee, task.trigger_reason) == (
        issue_url(1),
        None,
        "issue_mention",
    )
    requests = [
        request for request in github.requests if request.url.path != "/graphql"
    ]
    assert [request.url.path for request in requests] == [
        "/search/issues",
        "/search/issues",
        f"/repos/{REPO}/issues/1/comments",
    ]
    assert (
        requests[0].url.params["q"] == f"is:issue is:open user:{OWNER} mentions:{LOGIN}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete,total", [(True, 1), (False, 101)])
async def test_mention_search_never_treats_a_partial_result_as_complete(
    monkeypatch, caplog, incomplete, total
):
    service, github = _board(monkeypatch, items=[])
    github.on(
        "GET",
        "/search/issues",
        {"items": [], "incomplete_results": incomplete, "total_count": total},
    )
    assert await service.candidates() == []
    assert t("integrations.github.response.mention_search_incomplete") in caplog.text
    assert (
        t(
            "integrations.github.response.mention_discovery_failed",
            message=t("integrations.github.response.mention_search_incomplete"),
        )
        in caplog.text
    )


# --------------------------------------------------------------------------- #
#   Selection                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_default_todo_assigned_ticket_is_selected(monkeypatch):
    service, github = _board(monkeypatch, items=[_item(number=1, status="Todo")])

    task = await service.first_task()

    assert task is not None
    assert (task.id, task.url, task.title) == ("I1", issue_url(1), "issue 1")
    assert task.status == Task.READY
    assert task.assignee == "aiko"
    assert task.trigger_reason == "ready_lane"
    assert task.trace_attributes() == {
        "target.provider": "github",
        "target.label": "#1",
        "target.kind": "ticket",
        "target.title": "issue 1",
        "target.url": issue_url(1),
    }
    assert github.writes == []


@pytest.mark.asyncio
async def test_the_agent_field_assigns_by_its_name(monkeypatch):
    """A value of the field named Agent assigns the member it names; the same
    value in another field does not."""
    service, github = _board(
        monkeypatch,
        items=[
            _item(number=1, status="Todo", assignee=None, agent="⚙aiko"),
            _item(number=2, status="Todo", assignee=None, agent="⚙aiko", field="QA"),
            _item(number=3, status="Todo", assignee=None, agent="⚙other"),
        ],
    )

    [task] = await service.candidates()

    assert (task.url, task.assignee) == (issue_url(1), "aiko")
    assert github.writes == []


@pytest.mark.asyncio
async def test_project_tasks_are_ordered_by_creation_date(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(number=1, status="Todo", created_at="2026-01-03T00:00:00Z"),
            _item(number=2, status="Todo", created_at="2026-01-01T00:00:00Z"),
            _item(number=3, status="Todo", created_at="2026-01-02T00:00:00Z"),
        ],
    )

    tasks, _ = service._build_project_tasks(await service.get_all_tickets())

    assert [task.url for task in tasks] == [issue_url(2), issue_url(3), issue_url(1)]


@pytest.mark.asyncio
async def test_custom_ready_lane_is_selected(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="Ready")],
        lane_map={"ready": "Ready", "done": "Completed", "working": "Doing"},
    )

    task = await service.first_task()

    assert task is not None
    assert task.status == Task.READY


@pytest.mark.asyncio
async def test_done_lane_and_other_assignee_are_ignored(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(number=1, status="Done"),
            _item(number=2, status="Todo", assignee="other"),
        ],
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_mention_does_not_allow_unassigned_ready_ticket(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="Todo", assignee=None, body="Please ⚙aiko")],
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_working_ticket_runs_only_when_last_issue_comment_is_not_mine(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment("reviewer", "Please update")]},
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "issue_comment"

    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment(LOGIN, "Done")]},
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_assigned_working_ticket_without_comments_is_selected(monkeypatch):
    service, _ = _board(monkeypatch, items=[_item(number=1, status="In Progress")])

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "statuses"),
    [
        ("Backlog", ["Backlog", "Todo", "In Progress", "Done"]),
        ("Icebox", ["Todo", "In Progress", "Done", "Icebox"]),
    ],
)
async def test_an_assigned_ticket_outside_the_work_window_is_ignored(
    monkeypatch, status, statuses
):
    service, _ = _board(
        monkeypatch, items=[_item(number=1, status=status)], statuses=statuses
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_intermediate_review_lane_is_treated_as_working_without_config(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Review")],
        statuses=["Todo", "In Progress", "In Review", "Done"],
    )

    task = await service.first_task()

    assert task is not None
    assert task.status == Task.IN_PROGRESS
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_backlog_ticket_with_mention_is_ignored(monkeypatch):
    # Position wins outright: a lane before the ready lane is out of the work
    # window, so even an explicit mention does not pull it in.
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="Backlog", assignee=None, body="Please ⚙aiko")],
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_backlog_ticket_with_unhandled_comment_is_ignored(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="Backlog")],
        comments={1: [_comment("reviewer", "Please update")]},
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_merged_pr_does_not_move_ticket_when_ticket_is_not_mine(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress", assignee="other")],
        timeline={1: [_referring("closed", "2026-01-02T00:00:00Z", merged=True)]},
    )

    assert await service.first_task() is None
    assert service.moved == []


@pytest.mark.asyncio
async def test_closed_unmerged_pr_does_not_move_ticket_to_done(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        timeline={1: [_referring("closed", "2026-01-02T00:00:00Z")]},
    )

    assert await service.first_task() is None
    assert service.moved == []


@pytest.mark.asyncio
async def test_merged_pr_moves_ticket_to_done_without_triggering(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        timeline={1: [_referring("closed", "2026-01-02T00:00:00Z", merged=True)]},
    )

    assert await service.first_task() is None
    assert service.moved == [(issue_url(1), "done")]


@pytest.mark.asyncio
@pytest.mark.parametrize("comments", [[], [_comment(LOGIN, "Done for now.")]])
async def test_open_pr_keeps_an_answered_issue_out_of_selection(monkeypatch, comments):
    """The PR patrol takes over once the ticket's conversation is answered."""
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: comments},
        timeline={1: [_referring("open", "2026-01-02T00:00:00Z")]},
    )

    assert await service.first_task() is None
    assert service.moved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("mention", [False, True])
async def test_a_linked_pr_does_not_hide_a_ticket_conversation(
    monkeypatch, merged, mention
):
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress", body="⚙aiko" if mention else "")],
        comments={} if mention else {1: [_comment("human", "Also do this")]},
        timeline={
            1: [
                _referring(
                    "closed" if merged else "open",
                    "2026-01-02T00:00:00Z",
                    merged=merged,
                )
            ]
        },
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == ("issue_mention" if mention else "issue_comment")
    assert service.moved == []


@pytest.mark.asyncio
async def test_a_ready_ticket_is_not_taken_while_an_issue_it_is_blocked_by_is_open(
    monkeypatch,
):
    """GitHub counts the open issues one is blocked by on the board's read, so
    a blocked ready ticket is passed over unread; one already being worked on
    is not stopped by a blocker added meanwhile."""
    service, github = _board(
        monkeypatch,
        items=[
            _item(number=1, status="Todo", open_blockers=1),
            _item(number=2, status="In Progress", open_blockers=1),
        ],
        comments={2: [_comment("human", "Also do this")]},
    )

    [task] = await service.candidates()

    assert task.url == issue_url(2)
    assert f"GET /repos/{REPO}/issues/1/comments" not in github.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "state"),
    [
        # An open one wins over a newer closed or merged one.
        (
            [
                _referring("closed", "2026-01-04T00:00:00Z", merged=True),
                _referring("open", "2026-01-01T00:00:00Z"),
            ],
            "open",
        ),
        # Otherwise the latest updated decides.
        (
            [
                _referring("closed", "2026-01-01T00:00:00Z"),
                _referring("closed", "2026-01-03T00:00:00Z", merged=True),
            ],
            "merged",
        ),
        ([{"event": "labeled"}], None),
    ],
)
async def test_the_related_pull_request_prefers_an_open_one_then_the_latest(
    monkeypatch, events, state
):
    service, _ = _board(
        monkeypatch, items=[_item(number=1, status="Todo")], timeline={1: events}
    )

    assert (
        await service._related_pull_request_state(service.item(issue_url(1), "ticket"))
        == state
    )


@pytest.mark.asyncio
async def test_related_pull_request_timeline_paginates(monkeypatch):
    service, github = _board(
        monkeypatch,
        items=[_item(number=1, status="Todo")],
        timeline={
            1: [{"event": "labeled"}] * 100
            + [_referring("open", "2026-01-08T00:00:00Z")]
        },
    )

    state = await service._related_pull_request_state(
        service.item(issue_url(1), "ticket")
    )

    assert state == "open"
    assert [
        (r.url.path, r.url.params["page"], r.url.params["per_page"])
        for r in github.requests
    ] == [
        (f"/repos/{REPO}/issues/1/timeline", "1", "100"),
        (f"/repos/{REPO}/issues/1/timeline", "2", "100"),
    ]


@pytest.mark.asyncio
async def test_a_timeline_that_cannot_be_read_fails_the_patrol(monkeypatch):
    """A ticket whose pull requests are unknown is not taken for one without
    any: that would dispatch work an open pull request already follows."""
    service, github = _board(monkeypatch, items=[_item(number=1, status="In Progress")])
    github.on(
        "GET", f"/repos/{REPO}/issues/1/timeline", httpx.Response(502, text="down")
    )

    with pytest.raises(ServiceError):
        await service.first_task()


@pytest.mark.asyncio
async def test_ready_ticket_is_selected_when_my_comment_predates_the_assignment(
    monkeypatch,
):
    """Reproduces issue #392.

    The member commented as the issue's author days before a human assigned
    them. That earlier comment says nothing about the assigned work, so it must
    not suppress the run.
    """
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={
            1: [
                _comment(
                    LOGIN,
                    "I filed this and refined the body.",
                    "2026-01-03T00:00:00Z",
                )
            ]
        },
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"
    # The agent still sees the full history even though it was excluded above.
    assert [comment.content for comment in task.comments] == [
        "I filed this and refined the body."
    ]


@pytest.mark.asyncio
async def test_ready_ticket_is_skipped_when_my_comment_follows_the_assignment(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={1: [_comment(LOGIN, "Done, opened the PR.", "2026-01-06T00:00:00Z")]},
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_reassignment_makes_an_earlier_finished_ticket_actionable_again(
    monkeypatch,
):
    """Re-assigning is how a human asks for the work to resume."""
    comments = {1: [_comment(LOGIN, "Done, opened the PR.", "2026-01-06T00:00:00Z")]}

    def assigned_at(when: str) -> dict:
        return _item(
            number=1,
            status="In Progress",
            assignee=None,
            agent="⚙aiko",
            agent_updated_at=when,
        )

    before, _ = _board(
        monkeypatch, items=[assigned_at("2026-01-05T00:00:00Z")], comments=comments
    )
    assert await before.first_task() is None

    after, _ = _board(
        monkeypatch, items=[assigned_at("2026-01-07T00:00:00Z")], comments=comments
    )
    task = await after.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_assignee_assignment_time_comes_from_the_assigned_event(monkeypatch):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assigned_events=[
                    {"login": LOGIN, "created_at": "2026-01-05T00:00:00Z"}
                ],
            )
        ],
        comments={
            1: [
                _comment(
                    LOGIN, "Reviewed this for someone else.", "2026-01-03T00:00:00Z"
                )
            ]
        },
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_assigned_event_for_another_user_does_not_date_my_assignment(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assigned_events=[
                    {"login": "someone-else", "created_at": "2026-01-05T00:00:00Z"}
                ],
            )
        ],
        comments={1: [_comment(LOGIN, "Done.", "2026-01-03T00:00:00Z")]},
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_working_ticket_with_only_pre_assignment_comments_is_working_lane(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="In Progress",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={1: [_comment("reviewer", "Old discussion", "2026-01-03T00:00:00Z")]},
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "created_at", "selected"),
    [
        # A failure after the assignment holds the ticket until someone speaks.
        ("failed", "2026-01-06T00:00:00Z", False),
        # One before it says nothing about the assigned work.
        ("failed", "2026-01-03T00:00:00Z", True),
        # A rate limit with no known reset holds it for good.
        ("rate_limited", "2026-01-06T00:00:00Z", False),
    ],
)
async def test_the_members_workflow_status_holds_a_ready_ticket(
    monkeypatch, reason, created_at, selected
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={1: [_comment(LOGIN, _status_comment(reason), created_at)]},
    )

    task = await service.first_task()

    assert (task is not None) is selected


@pytest.mark.asyncio
async def test_unknown_assignment_time_keeps_every_comment(monkeypatch):
    """Without an assignment time nothing can be excluded, so the brake holds."""
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment(LOGIN, "Done")]},
    )

    assert await service.first_task() is None


@pytest.mark.asyncio
async def test_mention_after_the_assignment_overrides_my_own_last_comment(
    monkeypatch,
):
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={
            1: [
                _comment(
                    "human", "⚙aiko please take another look", "2026-01-06T00:00:00Z"
                ),
                _comment(LOGIN, "Looking now.", "2026-01-07T00:00:00Z"),
                _comment("human", "⚙aiko one more thing", "2026-01-08T00:00:00Z"),
            ]
        },
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_mention_on_second_comment_page_reopens_the_ticket(monkeypatch):
    service, github = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        comments={
            1: [
                _comment("human", f"discussion {index}", "2026-01-06T00:00:00Z")
                for index in range(99)
            ]
            + [
                _comment(LOGIN, "Done.", "2026-01-07T00:00:00Z"),
                _comment("human", "⚙aiko one more thing", "2026-01-08T00:00:00Z"),
            ]
        },
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"
    assert task.comments[-1].content == "⚙aiko one more thing"
    assert [
        r.url.params["page"]
        for r in github.requests
        if r.url.path.endswith("/comments")
    ] == ["1", "2"]


@pytest.mark.asyncio
async def test_latest_assignment_wins_when_both_paths_assign_this_member(
    monkeypatch,
):
    """Assignee and Agent field are both valid ways to ask; the newest asks."""
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="In Progress",
                assigned_events=[
                    {"login": LOGIN, "created_at": "2026-01-05T00:00:00Z"}
                ],
                agent="⚙aiko",
                agent_updated_at="2026-01-07T00:00:00Z",
            )
        ],
        comments={1: [_comment(LOGIN, "Done, opened the PR.", "2026-01-06T00:00:00Z")]},
    )

    task = await service.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_refresh_uses_current_comments_and_trigger_reason(monkeypatch):
    service, github = _board(monkeypatch, items=[_item(number=1, status="In Progress")])
    candidate = (await service.candidates())[0]
    endpoint = f"/repos/{REPO}/issues/1/comments"

    github.on(
        "GET",
        endpoint,
        pages([_comment("human", "Please update this too", "2026-01-02T00:00:00Z")]),
    )
    refreshed = await service.refresh(candidate)

    assert refreshed is not None
    assert refreshed.trigger_reason == "issue_comment"

    github.on(
        "GET", endpoint, pages([_comment(LOGIN, "Handled", "2026-01-03T00:00:00Z")])
    )
    assert await service.refresh(candidate) is None


@pytest.mark.asyncio
async def test_app_member_is_recognized_under_the_graphql_login(monkeypatch):
    """The board reads GraphQL, which names an App ``<app>``; the member's
    username is the REST form ``<app>[bot]``. Both assignment paths and the
    assignment time must still find the member."""
    service, _ = _board(
        monkeypatch,
        items=[
            _item(
                number=1,
                status="Todo",
                assignee="aiko-app",
                assigned_events=[
                    {"login": "aiko-app", "created_at": "2026-01-05T00:00:00Z"}
                ],
            )
        ],
        comments={
            1: [_comment("aiko-app[bot]", "Done earlier", "2026-01-04T00:00:00Z")]
        },
        github_username="aiko-app[bot]",
    )

    task = await service.first_task()

    assert task is not None
    assert task.assignee == "aiko"
    # The comment predates the assignment, so it is not this member's answer.
    assert task.trigger_reason == "ready_lane"
    assert task.comments[0].author_type == Message.ASSISTANT
    assert service._mentions_me(service._mentions(rendered_body("@aiko-app please")))


# --------------------------------------------------------------------------- #
#   The board's fields                                                        #
# --------------------------------------------------------------------------- #


def _fields(options: list[dict] | None) -> dict:
    """The Project's fields with the Agent field's ``options``; ``None``
    means no Agent field."""
    nodes = []
    if options is not None:
        nodes.append({"id": "agent-field-id", "name": AGENT_FIELD, "options": options})
    return {
        "node": {
            "fields": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


def _agent_service(github: ScriptedGitHub, monkeypatch, *agents: tuple[str, str]):
    """A ticket service whose team has the given non-human members (the Agent
    field's options), with the Project's node known."""
    others = [
        member().model_copy(
            update={
                "person_id": person_id,
                "name": name,
                "person_type": "machine_user",
                "account_info": {"github_username": person_id},
            }
        )
        for person_id, name in agents
    ]
    service = ticket_service(github, monkeypatch, member(), *others)
    service._project_node_id = "agent-proj"
    return service


@pytest.mark.asyncio
async def test_get_agent_field_state_reports_missing_field(monkeypatch):
    github = ScriptedGitHub().on_graphql(_FIELDS, _fields(None))
    service = _agent_service(github, monkeypatch, ("bot1", "Bot One"))

    state = await service.get_agent_field_state()

    assert state["exists"] is False
    assert state["options"] == []
    assert state["missing"] == [
        {"name": "⚙aiko", "description": "Aiko"},
        {"name": "⚙bot1", "description": "Bot One"},
    ]


@pytest.mark.asyncio
async def test_get_agent_field_state_lists_registered_and_missing(monkeypatch):
    github = ScriptedGitHub().on_graphql(
        _FIELDS,
        _fields(
            [
                {"id": "o0", "name": "⚙aiko", "description": "Aiko"},
                {"id": "o1", "name": "⚙bot1", "description": "Bot One"},
            ]
        ),
    )
    service = _agent_service(
        github, monkeypatch, ("bot1", "Bot One"), ("bot2", "Bot Two")
    )

    state = await service.get_agent_field_state()

    assert state["exists"] is True
    assert state["options"] == [
        {"name": "⚙aiko", "description": "Aiko"},
        {"name": "⚙bot1", "description": "Bot One"},
    ]
    assert state["missing"] == [{"name": "⚙bot2", "description": "Bot Two"}]


@pytest.mark.asyncio
async def test_sync_agent_field_creates_field_with_options(monkeypatch):
    created: list[dict] = []
    github = (
        ScriptedGitHub()
        .on_graphql(
            "createProjectV2Field",
            lambda variables: (
                created.append(variables)
                or {"createProjectV2Field": {"projectV2Field": {"id": "f1"}}}
            ),
        )
        .on_graphql(
            _FIELDS,
            lambda _: _fields(
                [{"id": "o1", "name": "⚙aiko", "description": "Aiko"}]
                if created
                else None
            ),
        )
    )
    service = _agent_service(github, monkeypatch)

    state = await service.sync_agent_field()

    [variables] = created
    assert variables["proj"] == "agent-proj"
    assert variables["options"] == [
        {"name": "⚙aiko", "description": "Aiko", "color": "GRAY"}
    ]
    assert state["exists"] is True
    assert state["missing"] == []


@pytest.mark.asyncio
async def test_sync_agent_field_adds_missing_option_preserving_existing(monkeypatch):
    existing = {"id": "o0", "name": "⚙aiko", "description": "Aiko", "color": "BLUE"}
    submitted: list[dict] = []
    github = (
        ScriptedGitHub()
        .on_graphql(
            "updateProjectV2Field(input",
            lambda variables: (
                submitted.append(variables)
                or {
                    "updateProjectV2Field": {"projectV2Field": {"id": "agent-field-id"}}
                }
            ),
        )
        .on_graphql(
            _FIELDS,
            lambda _: _fields(
                [existing]
                + (
                    [{"id": "o1", "name": "⚙bot1", "description": "Bot One"}]
                    if submitted
                    else []
                )
            ),
        )
    )
    service = _agent_service(github, monkeypatch, ("bot1", "Bot One"))

    state = await service.sync_agent_field()

    # The existing option is resubmitted WITH its id (preserving assignments);
    # the new one is appended without an id.
    [variables] = submitted
    assert variables == {
        "field": "agent-field-id",
        "options": [
            existing,
            {"name": "⚙bot1", "description": "Bot One", "color": "GRAY"},
        ],
    }
    assert state["exists"] is True
    assert state["missing"] == []


@pytest.mark.asyncio
async def test_is_assignable_user_resolves_existing_account(monkeypatch):
    github = ScriptedGitHub().on_graphql(
        "user(login",
        lambda variables: (
            {"user": {"id": "U1"}} if variables["login"] == LOGIN else {"user": None}
        ),
    )
    service = ticket_service(github, monkeypatch)

    assert await service.is_assignable_user(LOGIN) is True
    assert await service.is_assignable_user("ghost") is False
    # An empty username never reaches GitHub.
    assert await service.is_assignable_user("") is False
    assert len(github.requests) == 2


@pytest.mark.asyncio
async def test_is_assignable_user_returns_false_on_graphql_error(monkeypatch):
    github = ScriptedGitHub().on_graphql(
        "user(login", httpx.Response(200, json={"errors": [{"type": "FORBIDDEN"}]})
    )
    service = ticket_service(github, monkeypatch)

    assert await service.is_assignable_user(LOGIN) is False


# --------------------------------------------------------------------------- #
#   Tickets                                                                   #
# --------------------------------------------------------------------------- #


def _issue(number: int, **fields: Any) -> dict[str, Any]:
    return {
        "number": number,
        "node_id": f"I_{number}",
        "title": f"issue {number}",
        "body": "body",
        "state": "open",
        "html_url": issue_url(number),
        "user": {"login": "human"},
        "assignees": [{"login": LOGIN}],
        "labels": [{"name": "bug"}],
        **fields,
    }


#: Issue #7 of the repository, and #8, which is a pull request: GitHub numbers
#: both in one sequence and answers a pull request at an issue's address too.
def _issues() -> ScriptedGitHub:
    return (
        ScriptedGitHub()
        .on("GET", f"/repos/{REPO}/issues/7", _issue(7))
        .on("GET", f"/repos/{REPO}/issues/8", _issue(8, pull_request={}))
        .on("GET", f"/repos/{REPO}/labels", pages([{"name": "bug"}, {"name": "Docs"}]))
        .on_graphql(
            NODE_REPOSITORY,
            {"node": {"repository": {"name": "demo", "owner": {"login": OWNER}}}},
        )
    )


def _status_field(*names: str) -> dict:
    return {
        "node": {
            "fields": {
                "nodes": [
                    {
                        "id": "status-field",
                        "name": "Status",
                        "options": [{"id": f"opt-{n}", "name": n} for n in names],
                    }
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


def _connection(*nodes: dict, more: bool = False) -> dict:
    return {"nodes": list(nodes), "pageInfo": {"hasNextPage": more}}


def _on_project(*items: dict, more: bool = False, **relations: Any) -> dict:
    """The issue's project items, and the issues it is related to."""
    return {
        "repository": {
            "issue": {
                "projectItems": _connection(*items, more=more),
                "parent": None,
                "subIssues": _connection(),
                "blockedBy": _connection(),
                "blocking": _connection(),
                **relations,
            }
        }
    }


def _relative(number: int, state: str = "OPEN", repository: str = REPO) -> dict:
    """An issue as the read of a related one names it."""
    return {
        "url": issue_url(number, repository),
        "title": f"issue {number}",
        "number": number,
        "state": state,
    }


def _project_item(project: str, status: str | None) -> dict:
    return {
        "project": {"id": project},
        "fieldValueByName": None if status is None else {"name": status},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("items", "lane"),
    [
        ([], None),
        ([_project_item("ANOTHER", "Todo")], None),
        ([_project_item("PROJECT", "Todo")], "ready"),
        ([_project_item("PROJECT", "In Progress")], "in_progress"),
        ([_project_item("PROJECT", "In Review")], "in_progress"),
        ([_project_item("PROJECT", "Done")], "done"),
        # On the board, but outside the work window or without a Status.
        ([_project_item("PROJECT", "Backlog")], "new"),
        ([_project_item("PROJECT", None)], "new"),
    ],
)
async def test_a_ticket_reads_its_lane_on_the_configured_board(
    monkeypatch, items, lane
):
    github = (
        _issues()
        .on_graphql("projectItems(first: 100)", _on_project(*items))
        .on_graphql(
            _FIELDS,
            _status_field("Backlog", "Todo", "In Progress", "In Review", "Done"),
        )
    )
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"

    page = await service.read("tickets", issue_url(7))

    target = WorkTarget(kind="ticket", url=issue_url(7), title="issue 7", label="#7")
    assert page.target == target
    assert page.items == [
        Ticket(
            **target.model_dump(),
            key="7",
            tracker=REPO,
            repository=REPO,
            body="body",
            state="open",
            assignees=[LOGIN],
            labels=["bug"],
            lane=lane,
            parent=None,
            children=[],
            blocked_by=[],
            blocks=[],
        ).model_dump()
    ]
    [lookup] = [b for b in github.documents() if "projectItems" in b["query"]]
    assert lookup["variables"] == {"owner": OWNER, "repo": "demo", "number": 7}


@pytest.mark.asyncio
async def test_a_ticket_reads_the_issues_it_is_related_to(monkeypatch):
    """The relations come with the board's read of the issue, in one request,
    each related issue with its state as the REST reads spell it."""
    github = _issues().on_graphql(
        "projectItems(first: 100)",
        _on_project(
            parent=_relative(1),
            subIssues=_connection(_relative(2, "CLOSED"), _relative(3)),
            blockedBy=_connection(_relative(4, repository="acme/other")),
            blocking=_connection(_relative(5)),
        ),
    )
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"

    [ticket] = (await service.read("tickets", issue_url(7))).items

    def relative(number: int, state: str = "open", repository: str = REPO) -> dict:
        return {
            "kind": "ticket",
            "url": issue_url(number, repository),
            "title": f"issue {number}",
            "label": f"#{number}",
            "state": state,
        }

    assert {
        key: ticket[key] for key in ("parent", "children", "blocked_by", "blocks")
    } == {
        "parent": relative(1),
        "children": [relative(2, "closed"), relative(3)],
        "blocked_by": [relative(4, repository="acme/other")],
        "blocks": [relative(5)],
    }
    assert [b for b in github.documents() if "blockedBy" in b["query"]] == [
        b for b in github.documents() if "projectItems" in b["query"]
    ]


@pytest.mark.asyncio
async def test_a_ticket_related_to_more_issues_than_one_page_is_not_guessed(
    monkeypatch,
):
    github = _issues().on_graphql(
        "projectItems(first: 100)", _on_project(blocking=_connection(more=True))
    )
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"

    with pytest.raises(ServiceError):
        await service.read("tickets", issue_url(7))


@pytest.mark.asyncio
async def test_a_ticket_on_more_projects_than_one_page_is_not_guessed(monkeypatch):
    github = _issues().on_graphql("projectItems(first: 100)", _on_project(more=True))
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"

    with pytest.raises(ServiceError):
        await service.read("tickets", issue_url(7))


@pytest.mark.asyncio
async def test_create_ticket_uses_the_trackers_own_labels(monkeypatch):
    github = _issues().on(
        "POST",
        f"/repos/{REPO}/issues",
        lambda request: {
            **_issue(12, title="New", labels=[{"name": "bug"}, {"name": "Docs"}]),
        },
    )
    service = ticket_service(github, monkeypatch)

    created = await service.create_ticket(
        REPO, "New", "Body", ["BUG", "docs", "bug"], add_to_board=False
    )

    assert github.bodies("POST", f"/repos/{REPO}/issues") == [
        {"title": "New", "body": "Body", "labels": ["bug", "Docs"]}
    ]
    assert created.labels == ["bug", "Docs"]
    assert created.target == WorkTarget(
        kind="ticket", url=issue_url(12), title="New", label="#12"
    )


@pytest.mark.asyncio
async def test_create_ticket_refuses_a_label_the_tracker_lacks_before_writing(
    monkeypatch,
):
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceError, match="'ghost' is not defined"):
        await service.create_ticket(REPO, "New", "Body", ["ghost"])

    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tracker", "error"),
    [
        ("other/demo", ServiceError),
        ("acme", ServiceNotFound),
        ("a/b/c", ServiceNotFound),
    ],
)
async def test_create_ticket_writes_only_to_a_tracker_in_scope(
    monkeypatch, tracker, error
):
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(error):
        await service.create_ticket(tracker, "New", "Body", [])

    assert github.writes == []


@pytest.mark.asyncio
async def test_update_ticket_changes_labels_one_by_one(monkeypatch):
    """Labels are added and removed by name, so labels a human set meanwhile
    survive; removing one the issue does not carry changes nothing."""
    after = _issue(7, labels=[{"name": "bug"}, {"name": "Docs"}], state="closed")
    answers = iter([_issue(7), after])
    github = (
        _issues()
        .on("GET", f"/repos/{REPO}/issues/7", lambda _: next(answers))
        .on("PATCH", f"/repos/{REPO}/issues/7", _issue(7, state="closed"))
        .on("POST", f"/repos/{REPO}/issues/7/labels", [])
        # "wontfix" is not on the issue: GitHub answers the DELETE with 404.
    )
    service = ticket_service(github, monkeypatch)

    updated = await service.update_ticket(
        issue_url(7),
        body=None,
        title="Renamed",
        add_labels=["docs"],
        remove_labels=["wontfix", "needs review"],
        state="closed",
        state_reason="completed",
    )

    assert github.writes == [
        f"PATCH /repos/{REPO}/issues/7",
        f"DELETE /repos/{REPO}/issues/7/labels/wontfix",
        f"DELETE /repos/{REPO}/issues/7/labels/needs review",
        f"POST /repos/{REPO}/issues/7/labels",
    ]
    # A label's name is one path segment, whatever it holds.
    assert github.requests[-3].url.raw_path.endswith(b"/labels/needs%20review")
    assert github.bodies("PATCH", f"/repos/{REPO}/issues/7") == [
        {"title": "Renamed", "state": "closed", "state_reason": "completed"}
    ]
    assert github.bodies("POST", f"/repos/{REPO}/issues/7/labels") == [
        {"labels": ["Docs"]}
    ]
    assert (updated.state, updated.state_changed, updated.labels) == (
        "closed",
        True,
        ["bug", "Docs"],
    )
    assert updated.target.label == "#7"


@pytest.mark.asyncio
async def test_update_ticket_reports_a_label_removal_github_refuses(monkeypatch):
    github = _issues().on(
        "DELETE",
        f"/repos/{REPO}/issues/7/labels/bug",
        httpx.Response(403, json={"message": "no"}),
    )
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceError, match=t("integrations.github.response.forbidden")):
        await service.update_ticket(
            issue_url(7),
            body=None,
            title=None,
            add_labels=[],
            remove_labels=["bug"],
            state=None,
            state_reason=None,
        )


@pytest.mark.asyncio
async def test_update_ticket_refuses_an_undefined_label_before_writing(monkeypatch):
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceError, match="'ghost' is not defined"):
        await service.update_ticket(
            issue_url(7),
            body="new",
            title=None,
            add_labels=["ghost"],
            remove_labels=["bug"],
            state=None,
            state_reason=None,
        )

    assert github.writes == []


def _relate(service: GitHubTicketService, url: str, **relations: Any):
    """Change only the relations of the issue ``url``."""
    return service.update_ticket(
        url,
        body=None,
        title=None,
        add_labels=[],
        remove_labels=[],
        state=None,
        state_reason=None,
        **relations,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "parent", "writes", "sent"),
    [
        # Into a parent: added to it by the child's id.
        (
            None,
            issue_url(1),
            [f"POST /repos/{REPO}/issues/1/sub_issues"],
            {"sub_issue_id": 7007, "replace_parent": False},
        ),
        # Moved from another parent, which GitHub does only when told to.
        (
            _relative(2),
            issue_url(1),
            [f"POST /repos/{REPO}/issues/1/sub_issues"],
            {"sub_issue_id": 7007, "replace_parent": True},
        ),
        # Out of its parent: removed from the parent its place names.
        (
            _relative(2),
            "",
            [f"DELETE /repos/{REPO}/issues/2/sub_issue"],
            {"sub_issue_id": 7007},
        ),
        # Already where it is asked to be, as GitHub spells either.
        (_relative(1, repository="ACME/Demo"), issue_url(1), [], None),
        (None, "", [], None),
    ],
)
async def test_a_tickets_parent_is_written_on_the_parent_by_the_childs_id(
    monkeypatch, current, parent, writes, sent
):
    github = (
        _issues()
        .on("GET", f"/repos/{REPO}/issues/7", _issue(7, id=7007))
        .on("GET", f"/repos/{REPO}/issues/1", _issue(1))
        .on_graphql("projectItems(first: 100)", _on_project(parent=current))
        .on("POST", f"/repos/{REPO}/issues/1/sub_issues", _issue(1))
        .on("DELETE", f"/repos/{REPO}/issues/2/sub_issue", _issue(2))
    )
    service = ticket_service(github, monkeypatch)

    await _relate(service, issue_url(7), parent=parent)

    assert github.writes == writes
    assert [
        json.loads(r.content)
        for r in github.requests
        if f"{r.method} {r.url.path}" in writes
    ] == ([sent] if sent else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [issue_url(1), ""])
async def test_leaving_a_parent_outside_the_owner_is_refused_and_writes_nothing(
    monkeypatch, parent
):
    """Moving the issue to another parent, or out of its parent, changes the
    parent it leaves too: one outside the owner refuses the whole update,
    before any of its other changes is sent."""
    github = (
        _issues()
        .on("GET", f"/repos/{REPO}/issues/7", _issue(7, id=7007))
        .on("GET", f"/repos/{REPO}/issues/1", _issue(1))
        .on_graphql(
            "projectItems(first: 100)",
            _on_project(parent=_relative(2, repository="other/demo")),
        )
    )
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceError, match="Writes are limited to"):
        await service.update_ticket(
            issue_url(7),
            body="new",
            title=None,
            add_labels=[],
            remove_labels=[],
            state=None,
            state_reason=None,
            parent=parent,
        )

    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relations",
    [
        {"parent": issue_url(404)},
        {"add_blocked_by": [issue_url(404)]},
        {"remove_blocked_by": [issue_url(404)]},
    ],
)
async def test_a_relation_to_an_issue_that_is_not_there_writes_nothing(
    monkeypatch, relations
):
    """Every related issue is read before the first write, so the update's
    other changes are not made either."""
    github = _issues().on_graphql("projectItems(first: 100)", _on_project())
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await service.update_ticket(
            issue_url(7),
            body="new",
            title=None,
            add_labels=[],
            remove_labels=[],
            state=None,
            state_reason=None,
            **relations,
        )

    assert github.writes == []


@pytest.mark.asyncio
async def test_a_tickets_blockers_are_written_by_their_ids(monkeypatch):
    """Adding one the issue is already blocked by (as GitHub spells it), or
    removing one it is not blocked by, changes nothing."""
    dependencies = f"/repos/{REPO}/issues/7/dependencies/blocked_by"
    github = (
        _issues()
        .on_graphql(
            "projectItems(first: 100)",
            _on_project(blockedBy=_connection(_relative(6, repository="ACME/Demo"))),
        )
        .on("GET", f"/repos/{REPO}/issues/3", _issue(3, id=3003))
        .on("GET", f"/repos/{REPO}/issues/4", _issue(4, id=4004))
        .on("GET", f"/repos/{REPO}/issues/5", _issue(5, id=5005))
        .on("POST", dependencies, _issue(3))
        .on("DELETE", f"{dependencies}/4004", _issue(4))
        # #5 does not block #7: GitHub answers the DELETE with 404.
    )
    service = ticket_service(github, monkeypatch)

    await _relate(
        service,
        issue_url(7),
        add_blocked_by=[issue_url(3), issue_url(6)],
        remove_blocked_by=[issue_url(4), issue_url(5)],
    )

    assert github.writes == [
        f"POST {dependencies}",
        f"DELETE {dependencies}/4004",
        f"DELETE {dependencies}/5005",
    ]
    assert github.bodies("POST", dependencies) == [{"issue_id": 3003}]
    assert f"GET /repos/{REPO}/issues/6" not in github.sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relations",
    [
        {"parent": issue_url(1, "other/demo")},
        {"add_blocked_by": [issue_url(3), issue_url(1, "other/demo")]},
        {"remove_blocked_by": [issue_url(1, "other/demo")]},
    ],
)
async def test_a_relation_to_an_issue_outside_the_owner_is_refused_unsent(
    monkeypatch, relations
):
    """A blocker is written on the member's own issue, where the gate would let
    it through, but the other issue shows it too: it is refused before any
    request, with the rest of the update."""
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceError, match="refused"):
        await service.update_ticket(
            issue_url(7),
            body="new",
            title=None,
            add_labels=[],
            remove_labels=[],
            state=None,
            state_reason=None,
            **relations,
        )

    assert github.requests == []


_TICKET_OPERATIONS = {
    "comment": lambda s, url: s.comment(url, "Hi"),
    "update": lambda s, url: s.update_ticket(
        url,
        body="new",
        title=None,
        add_labels=["bug"],
        remove_labels=["bug"],
        state="closed",
        state_reason=None,
    ),
    "reaction": lambda s, url: s.add_reaction(url, "issuecomment-5", "ack"),
    "read": lambda s, url: s.read("tickets", url),
    "comments": lambda s, url: s.read("ticket_comments", url),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(_TICKET_OPERATIONS))
async def test_a_pull_requests_number_is_no_ticket(monkeypatch, operation):
    """GitHub numbers issues and pull requests together and answers a pull
    request at an issue's address: a ticket operation refuses it before it
    writes anything."""
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceNotFound, match="is a pull request"):
        await _TICKET_OPERATIONS[operation](service, issue_url(8))

    assert github.writes == []
    assert all("/comments/" not in path for path in github.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(_TICKET_OPERATIONS))
@pytest.mark.parametrize(
    "url",
    [
        pull_url(7),
        f"https://github.example.com/{REPO}/issues/7",
        f"{issue_url(7)}#issuecomment-5",
        f"https://github.com/{REPO}/issues/0",
    ],
)
async def test_what_is_no_issue_url_is_refused_unread(monkeypatch, operation, url):
    github = _issues()
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await _TICKET_OPERATIONS[operation](service, url)

    assert github.requests == []


@pytest.mark.asyncio
async def test_a_status_comment_carries_the_status_and_addresses_the_author(
    monkeypatch,
):
    github = _issues().on(
        "POST",
        f"/repos/{REPO}/issues/7/comments",
        {
            "id": 100,
            "html_url": f"{issue_url(7)}#issuecomment-100",
            "user": {"login": LOGIN},
            "created_at": "2026-01-09T00:00:00Z",
        },
    )
    service = ticket_service(github, monkeypatch)
    status = WorkflowStatus(
        reason="rate_limited",
        person_id="aiko",
        run_id="run-1",
        subject=issue_url(7),
        retry_after_at="2026-01-10T00:00:00+00:00",
    )

    written = await service.comment(issue_url(7), "Paused.\n", status=status)

    [body] = github.bodies("POST", f"/repos/{REPO}/issues/7/comments")
    assert body["body"].startswith("@human\n\n<!-- ")
    assert body["body"].endswith("Paused.")
    assert parse_workflow_status_comment(body["body"]) == status
    assert written.id == "issuecomment-100"
    assert written.url == f"{issue_url(7)}#issuecomment-100"
    assert written.target == WorkTarget(
        kind="ticket", url=issue_url(7), title="issue 7", label="#7"
    )
    # The issue is read before the comment, never after it.
    assert github.sent[-1] == f"POST /repos/{REPO}/issues/7/comments"


@pytest.mark.asyncio
async def test_a_plain_comment_is_posted_as_written(monkeypatch):
    github = _issues().on(
        "POST", f"/repos/{REPO}/issues/7/comments", {"id": 101, "user": {}}
    )
    service = ticket_service(github, monkeypatch)

    await service.comment(issue_url(7), "@human thanks")

    assert github.bodies("POST", f"/repos/{REPO}/issues/7/comments") == [
        {"body": "@human thanks"}
    ]


def _issue_comment(number: int, on: str) -> dict:
    return {"id": number, "node_id": f"IC_{number}", "issue_url": on}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_url",
    [
        f"{API}/repos/{REPO}/issues/7",
        # GitHub spells the repository as it is named.
        f"{API}/repos/ACME/Demo/issues/7",
    ],
)
async def test_a_reaction_reaches_the_comment_of_the_ticket_by_its_id(
    monkeypatch, owner_url
):
    github = (
        _issues()
        .on("GET", f"/repos/{REPO}/issues/comments/5", _issue_comment(5, owner_url))
        .on_graphql(
            ADD_REACTION, {"addReaction": {"reaction": {"content": "THUMBS_UP"}}}
        )
    )
    service = ticket_service(github, monkeypatch)

    added = await service.add_reaction(issue_url(7), "issuecomment-5", "ack")

    assert (added.reaction, added.id) == ("ack", "issuecomment-5")
    reaction = github.documents()[-1]
    assert reaction == {
        "query": ADD_REACTION,
        "variables": {"subject": "IC_5", "content": "THUMBS_UP"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_id",
    [
        # Another issue's comment, read where its id says it is.
        "issuecomment-6",
        # Kinds a ticket has none of.
        "discussion_r5",
        "pullrequestreview-5",
        # Ids of no kind GitHub gives.
        "issuecomment-0",
        "issuecomment-abc",
        "5",
        "comment-5",
    ],
)
async def test_a_reaction_to_what_is_not_the_tickets_is_refused_before_it_is_sent(
    monkeypatch, target_id
):
    github = _issues().on(
        "GET",
        f"/repos/{REPO}/issues/comments/6",
        _issue_comment(6, f"{API}/repos/{REPO}/issues/9"),
    )
    service = ticket_service(github, monkeypatch)

    with pytest.raises(ServiceNotFound):
        await service.add_reaction(issue_url(7), target_id, "agree")

    assert github.writes == []
    assert github.documents() == []
    read = [p for p in github.sent if "/comments/" in p or "/reviews/" in p]
    assert read == (
        [f"GET /repos/{REPO}/issues/comments/6"] if target_id.endswith("-6") else []
    )


# --------------------------------------------------------------------------- #
#   The board                                                                 #
# --------------------------------------------------------------------------- #


def _board_github() -> ScriptedGitHub:
    return (
        _issues()
        .on_graphql(
            ADD_PROJECT_ITEM, {"addProjectV2ItemById": {"item": {"id": "ITEM_7"}}}
        )
        .on_graphql(UPDATE_PROJECT_ITEM_STATUS, {"updateProjectV2ItemFieldValue": {}})
        .on_graphql(_FIELDS, _status_field("Todo", "In Progress", "Done"))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lane", "option"),
    [("ready", "opt-Todo"), ("in_progress", "opt-In Progress"), ("done", "opt-Done")],
)
async def test_move_sets_the_lanes_status(monkeypatch, lane, option):
    github = _board_github()
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"
    task = Task(id="I_7", url=issue_url(7), title="issue 7", description="")

    assert await service.move(task, lane) is True

    [update] = [
        b for b in github.documents() if b["query"] == UPDATE_PROJECT_ITEM_STATUS
    ]
    assert update["variables"] == {
        "proj": "PROJECT",
        "item": "ITEM_7",
        "field": "status-field",
        "opt": option,
    }


@pytest.mark.asyncio
async def test_a_board_without_the_working_lane_leaves_the_ticket_where_it_is(
    monkeypatch,
):
    github = _board_github()
    service = ticket_service(github, monkeypatch, lane_map={"working": ""})
    service._project_node_id = "PROJECT"
    task = Task(id="I_7", url=issue_url(7), title="issue 7", description="")

    assert await service.move(task, "in_progress") is False

    assert UPDATE_PROJECT_ITEM_STATUS not in [b["query"] for b in github.documents()]
