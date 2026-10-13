"""GitHub as a member's tickets: the issues it reads and writes, and the
Projects board it selects work from."""

import logging
from typing import Any

import httpx
import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.integrations.github.repository_scope import (
    ADD_PROJECT_ITEM,
    ADD_REACTION,
    CREATE_PROJECT_FIELD,
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
    serve,
    team,
    ticket_service,
)

AGENT_FIELD = GitHubTicketService.FIELD_AGENT
#: What marks the read of the board's fields.
_FIELDS = "fields(first"


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
        },
    }


def _comment(user: str, body: str, created_at: str = "2026-01-01T00:00:00Z"):
    return {"user": {"login": user}, "body": body, "created_at": created_at}


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
async def test_the_patrol_leaves_a_missing_agent_field_to_setup(monkeypatch):
    """The board has no Agent field: the patrol still selects by assignee and
    creates nothing (the field is set up from the board settings)."""
    service, github = _board(monkeypatch, items=[_item(number=1, status="Todo")])
    github.on_graphql(
        CREATE_PROJECT_FIELD, {"createProjectV2Field": {"projectV2Field": {"id": "F"}}}
    )

    assert [task.url for task in await service.candidates()] == [issue_url(1)]

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
async def test_open_pr_keeps_its_issue_out_of_the_working_lane(monkeypatch):
    """Feedback on a PR is followed on the PR; the issue is never re-dispatched."""
    service, _ = _board(
        monkeypatch,
        items=[_item(number=1, status="In Progress")],
        comments={1: [_comment("human", "Also do this")]},
        timeline={1: [_referring("open", "2026-01-02T00:00:00Z")]},
    )

    assert await service.first_task() is None
    assert service.moved == []


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
        r.url.params["page"] for r in github.requests if r.url.path != "/graphql"
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
    assert service._text_mentions_me("@aiko-app please")


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


def _on_project(*items: dict, more: bool = False) -> dict:
    return {
        "repository": {
            "issue": {
                "projectItems": {
                    "nodes": list(items),
                    "pageInfo": {"hasNextPage": more},
                }
            }
        }
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
        ).model_dump()
    ]
    [lookup] = [b for b in github.documents() if "projectItems" in b["query"]]
    assert lookup["variables"] == {"owner": OWNER, "repo": "demo", "number": 7}


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

    created = await service.create_ticket(REPO, "New", "Body", ["BUG", "docs", "bug"])

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
    "board": lambda s, url: s.add(url),
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
        .on_graphql(ADD_REACTION, {"addReaction": {"reaction": {"content": "EYES"}}})
    )
    service = ticket_service(github, monkeypatch)

    added = await service.add_reaction(issue_url(7), "issuecomment-5", "ack")

    assert (added.reaction, added.id) == ("ack", "issuecomment-5")
    reaction = github.documents()[-1]
    assert reaction == {
        "query": ADD_REACTION,
        "variables": {"subject": "IC_5", "content": "EYES"},
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
async def test_add_puts_the_issue_on_the_configured_project(monkeypatch):
    github = _board_github()
    service = ticket_service(github, monkeypatch)
    service._project_node_id = "PROJECT"

    assert await service.add(issue_url(7)) is True

    assert [b["query"] for b in github.documents()] == [
        NODE_REPOSITORY,
        ADD_PROJECT_ITEM,
    ]
    assert github.documents()[-1]["variables"] == {"proj": "PROJECT", "content": "I_7"}


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
