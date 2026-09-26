import logging
from typing import Any

import pytest

from guildbotics.entities.message import Message
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.chat_workflow_status import workflow_status_fields
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.integrations.workflow_status_comment import (
    parse_workflow_status_comment,
    render_workflow_status_comment,
)
from guildbotics.utils.i18n_tool import set_language, t


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses
        self.response_sequences: dict[str, list[Any]] = {}
        self.gets: list[tuple[str, dict[str, Any]]] = []

    async def get(self, endpoint: str, **kwargs):
        self.gets.append((endpoint, kwargs))
        if endpoint in self.response_sequences:
            response = self.response_sequences[endpoint].pop(0)
            return response if isinstance(response, _Response) else _Response(response)
        return _Response(self.responses.get(endpoint, []))


class _Manager(GitHubTicketManager):
    def __init__(
        self,
        *,
        items: list[dict],
        responses: dict[str, Any] | None = None,
        lane_map: dict[str, str] | None = None,
        statuses: list[str] | None = None,
        github_username: str = "aiko-gh",
    ):
        person = Person(
            person_id="aiko",
            name="Aiko",
            account_info={"github_username": github_username},
        )
        services: dict[str, dict[str, Any]] = {
            "ticket_manager": {
                "name": "GitHub",
                "owner": "GuildBotics",
                "project_id": "1",
                "url": "https://github.com/orgs/GuildBotics/projects/1",
            }
        }
        if lane_map is not None:
            services["ticket_manager"]["lane_map"] = lane_map
        team = Team(
            project=Project(
                name="demo",
                services=services,
            ),
            members=[person],
        )
        super().__init__(logging.getLogger("test"), person, team)
        # Board column order (left -> right). Status options before the ready
        # lane or at/after the done lane are ignored; options between them are
        # treated as working lanes. Mirrors what _sync_status_columns() caches
        # from _get_status_field() in production.
        board = statuses if statuses is not None else ["Todo", "In Progress", "Done"]
        self._status_positions = {name: index for index, name in enumerate(board)}
        self.items = items
        self.client_stub = _Client(responses or {})
        self.custom_fields = {
            GitHubTicketManager.FIELD_AGENT: {
                "id": "agent-field",
                "name": GitHubTicketManager.FIELD_AGENT,
                "dataType": "SINGLE_SELECT",
                "options": {},
            }
        }
        self.moved: list[tuple[Task, str]] = []
        self.related_pulls: list[dict[str, Any]] = []
        # Pull request patrol inputs: search hits and the GraphQL node per PR.
        self.search_items: list[dict[str, Any]] = []
        self.pull_request_nodes: dict[int, dict[str, Any]] = {}
        self.comments_added: list[tuple[Task, str]] = []
        self.graphql_queries: list[str] = []

    async def login(self):
        return self.client_stub

    async def get_all_tickets(self):
        return self.items

    async def move_ticket(self, task: Task, new_status: str) -> bool:
        self.moved.append((task, new_status))
        return True

    async def _get_related_pull_requests(
        self, task: Task, issue_number: int
    ) -> list[dict[str, Any]]:
        return self.related_pulls

    async def _search_pull_requests(self) -> list[dict[str, Any]]:
        return self.search_items

    async def add_comment_to_ticket(self, task: Task, comment: str) -> None:
        self.comments_added.append((task, comment))

    async def _graphql(self, query: str, variables: dict) -> dict:
        self.graphql_queries.append(query)
        node = self.pull_request_nodes.get(int(variables.get("number") or 0))
        return {"repository": {"pullRequest": node}}

    async def first_task(self) -> Task | None:
        """Return the first patrol candidate for single-selection assertions."""
        candidates = await self.get_task_candidates()
        return candidates[0] if candidates else None


def _item(
    *,
    number: int,
    status: str,
    assignee: str | None = "aiko-gh",
    body: str = "",
    agent: str | None = None,
    agent_updated_at: str | None = None,
    assigned_events: list[dict[str, str]] | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
    extra_field_values: list[dict[str, Any]] | None = None,
) -> dict:
    field_values = [{"field": {"name": "Status"}, "name": status}]
    if agent:
        agent_value: dict[str, Any] = {
            "field": {
                "id": "agent-field",
                "name": GitHubTicketManager.FIELD_AGENT,
            },
            "name": agent,
        }
        if agent_updated_at:
            agent_value["updatedAt"] = agent_updated_at
        field_values.append(agent_value)
    field_values.extend(extra_field_values or [])
    assignees = [{"login": assignee}] if assignee else []
    timeline_nodes = [
        {
            "createdAt": event["created_at"],
            "assignee": {"login": event["login"]},
        }
        for event in assigned_events or []
    ]
    return {
        "fieldValues": {"nodes": field_values},
        "content": {
            "id": f"I{number}",
            "number": number,
            "title": f"issue {number}",
            "body": body,
            "createdAt": created_at,
            "assignees": {"nodes": assignees},
            "timelineItems": {"nodes": timeline_nodes},
            "repository": {"name": "repo", "owner": {"login": "GuildBotics"}},
        },
    }


def _comments(number: int, comments: list[dict[str, str]]) -> dict[str, Any]:
    return {
        f"/repos/GuildBotics/repo/issues/{number}/comments": [
            {
                "body": comment["body"],
                "created_at": comment.get("created_at", f"2026-01-01T00:0{index}:00Z"),
                "user": {"login": comment["user"]},
            }
            for index, comment in enumerate(comments)
        ]
    }


def _pull() -> dict[str, Any]:
    return {
        "url": "https://github.com/GuildBotics/repo/pull/2",
        "owner": "GuildBotics",
        "repo": "repo",
        "number": 2,
        "state": "open",
        "updated_at": "2026-01-02T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_default_todo_assigned_ticket_is_selected():
    manager = _Manager(items=[_item(number=1, status="Todo")])

    task = await manager.first_task()

    assert task is not None
    assert task.status == Task.READY
    assert task.assignee == "aiko"
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_single_select_priority_field_does_not_break_ticket_retrieval():
    """A board carrying a single-select ``Priority`` field is simply ignored."""
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                extra_field_values=[
                    {
                        "field": {"id": "priority-field", "name": "Priority"},
                        "name": "P1",
                    }
                ],
            )
        ]
    )
    manager.custom_fields["Priority"] = {
        "id": "priority-field",
        "name": "Priority",
        "dataType": "SINGLE_SELECT",
        "options": {},
    }

    task = await manager.first_task()

    assert task is not None
    assert task.status == Task.READY


def test_project_tasks_are_ordered_by_creation_date():
    manager = _Manager(
        items=[
            _item(number=1, status="Todo", created_at="2026-01-03T00:00:00Z"),
            _item(number=2, status="Todo", created_at="2026-01-01T00:00:00Z"),
            _item(number=3, status="Todo", created_at="2026-01-02T00:00:00Z"),
        ]
    )

    tasks, _ = manager._build_project_tasks(manager.items)

    assert [task.number for task in tasks] == [2, 3, 1]


@pytest.mark.asyncio
async def test_custom_ready_lane_is_selected():
    manager = _Manager(
        items=[_item(number=1, status="Ready")],
        lane_map={"ready": "Ready", "done": "Completed", "working": "Doing"},
    )

    task = await manager.first_task()

    assert task is not None
    assert task.status == Task.READY


@pytest.mark.asyncio
async def test_done_lane_and_other_assignee_are_ignored():
    manager = _Manager(
        items=[
            _item(number=1, status="Done"),
            _item(number=2, status="Todo", assignee="other"),
        ]
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_mention_does_not_allow_unassigned_ready_ticket():
    manager = _Manager(
        items=[_item(number=1, status="Todo", assignee=None, body="Please ⚙aiko")]
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_working_ticket_runs_only_when_last_issue_comment_is_not_mine():
    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "reviewer", "body": "Please update"}]),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "issue_comment"

    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "aiko-gh", "body": "Done"}]),
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_assigned_working_ticket_without_comments_is_selected():
    manager = _Manager(items=[_item(number=1, status="In Progress")])

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_assigned_backlog_ticket_before_ready_lane_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Backlog")],
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_assigned_icebox_ticket_after_done_lane_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Icebox")],
        statuses=["Todo", "In Progress", "Done", "Icebox"],
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_intermediate_review_lane_is_treated_as_working_without_config():
    manager = _Manager(
        items=[_item(number=1, status="In Review")],
        statuses=["Todo", "In Progress", "In Review", "Done"],
    )

    task = await manager.first_task()

    assert task is not None
    assert task.status == Task.IN_PROGRESS
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_backlog_ticket_with_mention_is_ignored():
    # Position wins outright: a lane before the ready lane is out of the work
    # window, so even an explicit mention does not pull it in.
    manager = _Manager(
        items=[_item(number=1, status="Backlog", assignee=None, body="Please ⚙aiko")],
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_backlog_ticket_with_unhandled_comment_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Backlog")],
        responses=_comments(1, [{"user": "reviewer", "body": "Please update"}]),
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_merged_pr_does_not_move_ticket_when_ticket_is_not_mine():
    manager = _Manager(items=[_item(number=1, status="In Progress", assignee="other")])
    manager.related_pulls = [
        {
            "url": "https://github.com/GuildBotics/repo/pull/2",
            "owner": "GuildBotics",
            "repo": "repo",
            "number": 2,
            "state": "merged",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    ]

    assert await manager.first_task() is None
    assert manager.moved == []


@pytest.mark.asyncio
async def test_closed_unmerged_pr_does_not_move_ticket_to_done():
    manager = _Manager(items=[_item(number=1, status="In Progress")])
    manager.related_pulls = [
        {
            "url": "https://github.com/GuildBotics/repo/pull/2",
            "owner": "GuildBotics",
            "repo": "repo",
            "number": 2,
            "state": "closed",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    ]

    assert await manager.first_task() is None
    assert manager.moved == []


@pytest.mark.asyncio
async def test_merged_pr_moves_ticket_to_done_without_triggering():
    manager = _Manager(items=[_item(number=1, status="In Progress")])
    manager.related_pulls = [
        {
            "url": "https://github.com/GuildBotics/repo/pull/2",
            "owner": "GuildBotics",
            "repo": "repo",
            "number": 2,
            "state": "merged",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    ]

    assert await manager.first_task() is None
    assert manager.moved and manager.moved[0][1] == Task.DONE


def test_select_related_pr_prefers_open_then_latest():
    manager = _Manager(items=[])
    pulls = [
        {"state": "closed", "updated_at": "2026-01-04T00:00:00Z", "url": "closed"},
        {"state": "open", "updated_at": "2026-01-01T00:00:00Z", "url": "open-old"},
        {"state": "open", "updated_at": "2026-01-03T00:00:00Z", "url": "open-new"},
    ]
    manager.related_pulls = pulls

    async def run():
        task = Task(id="I1", title="T", description="D")
        return await manager._select_related_pull_request(task, 1)

    import asyncio

    assert asyncio.run(run())["url"] == "open-new"


@pytest.mark.asyncio
async def test_related_pull_request_timeline_paginates():
    manager = _Manager(
        items=[],
        responses={
            "/repos/GuildBotics/repo/pulls/2": {
                "html_url": "https://github.com/GuildBotics/repo/pull/2",
                "state": "open",
                "merged_at": None,
                "updated_at": "2026-01-08T00:00:00Z",
            }
        },
    )
    endpoint = "/repos/GuildBotics/repo/issues/1/timeline"
    manager.client_stub.response_sequences[endpoint] = [
        [{"event": "labeled"} for _ in range(100)],
        [
            {
                "source": {
                    "issue": {
                        "pull_request": {
                            "url": "https://api.github.com/repos/GuildBotics/repo/pulls/2"
                        },
                        "html_url": "https://github.com/GuildBotics/repo/pull/2",
                    }
                }
            }
        ],
    ]
    task = Task(id="I1", title="T", description="D", repository="repo")

    pulls = await GitHubTicketManager._get_related_pull_requests(manager, task, 1)

    assert [pull["url"] for pull in pulls] == [
        "https://github.com/GuildBotics/repo/pull/2"
    ]
    assert manager.client_stub.gets[:2] == [
        (
            endpoint,
            {
                "params": {"per_page": 100, "page": 1},
                "headers": {"Accept": "application/vnd.github+json"},
            },
        ),
        (
            endpoint,
            {
                "params": {"per_page": 100, "page": 2},
                "headers": {"Accept": "application/vnd.github+json"},
            },
        ),
    ]


@pytest.mark.asyncio
async def test_related_pull_requests_keep_results_before_a_later_page_fails():
    manager = _Manager(
        items=[],
        responses={
            "/repos/GuildBotics/repo/pulls/2": {
                "html_url": "https://github.com/GuildBotics/repo/pull/2",
                "state": "open",
                "merged_at": None,
                "updated_at": "2026-01-08T00:00:00Z",
            }
        },
    )
    endpoint = "/repos/GuildBotics/repo/issues/1/timeline"
    linked_event = {
        "source": {
            "issue": {
                "pull_request": {
                    "url": "https://api.github.com/repos/GuildBotics/repo/pulls/2"
                },
                "html_url": "https://github.com/GuildBotics/repo/pull/2",
            }
        }
    }
    manager.client_stub.response_sequences[endpoint] = [
        [linked_event, *({"event": "labeled"} for _ in range(99))],
        _Response([], status_code=502),
    ]
    task = Task(id="I1", title="T", description="D", repository="repo")

    pulls = await GitHubTicketManager._get_related_pull_requests(manager, task, 1)

    assert [pull["url"] for pull in pulls] == [
        "https://github.com/GuildBotics/repo/pull/2"
    ]


def _script_graphql(manager: _Manager, handler) -> list[tuple[str, dict]]:
    """Replace ``_graphql`` with a scripted handler and record its calls."""
    calls: list[tuple[str, dict]] = []

    async def fake_graphql(query: str, variables: dict) -> dict:
        calls.append((query, variables))
        return handler(query, variables)

    manager._graphql = fake_graphql  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_is_assignable_user_resolves_existing_account():
    manager = _Manager(items=[])
    _script_graphql(
        manager,
        lambda query, variables: (
            {"user": {"id": "U1"}}
            if variables["login"] == "aiko-gh"
            else {"user": None}
        ),
    )

    assert await manager.is_assignable_user("aiko-gh") is True
    assert await manager.is_assignable_user("ghost") is False
    # An empty username never hits the API.
    assert await manager.is_assignable_user("") is False


@pytest.mark.asyncio
async def test_is_assignable_user_returns_false_on_graphql_error():
    manager = _Manager(items=[])

    async def boom(query: str, variables: dict) -> dict:
        raise RuntimeError("GraphQL failed")

    manager._graphql = boom  # type: ignore[method-assign]

    assert await manager.is_assignable_user("aiko-gh") is False


@pytest.mark.asyncio
async def test_get_ticket_url_for_draft_links_to_project_board():
    manager = _Manager(items=[])
    # A draft has no repository, so it cannot have a per-issue URL; it links to
    # the configured Project board instead and never resolves an issue number.
    manager._get_issue_number = lambda node_id: _coro(  # type: ignore[assignment]
        pytest.fail("draft tickets must not resolve an issue number")
    )

    task = Task(id="ITEM1", title="Proposal", description="", status=Task.READY)
    url = await manager.get_ticket_url(task, markdown=False)

    assert url == "https://github.com/orgs/GuildBotics/projects/1"
    markdown = await manager.get_ticket_url(task, markdown=True)
    assert markdown == "[Proposal](https://github.com/orgs/GuildBotics/projects/1)"


def _coro(value: Any):
    async def _inner() -> Any:
        return value

    return _inner()


def _agent_manager(*agent_members: tuple[str, str]) -> GitHubTicketManager:
    """A manager whose team has the given non-human members (Agent options)."""
    person = Person(
        person_id="aiko", name="Aiko", account_info={"github_username": "aiko-gh"}
    )
    members: list[Person] = [person]
    for person_id, name in agent_members:
        members.append(
            Person(
                person_id=person_id,
                name=name,
                person_type="machine_user",
                account_info={"github_username": person_id},
            )
        )
    services = {
        "ticket_manager": {
            "name": "GitHub",
            "owner": "GuildBotics",
            "project_id": "1",
            "url": "https://github.com/orgs/GuildBotics/projects/1",
        }
    }
    team = Team(project=Project(name="demo", services=services), members=members)
    manager = GitHubTicketManager(logging.getLogger("test"), person, team)
    # Pre-cache the project node id so helpers don't issue a _project_node query.
    manager._project_node_id = "agent-proj"
    return manager


def _fields_payload(options: list[dict] | None) -> dict:
    """Build a ``_get_custom_fields`` response; ``None`` means no Agent field."""
    nodes = []
    if options is not None:
        nodes.append(
            {
                "id": "agent-field-id",
                "name": "Agent",
                "dataType": "SINGLE_SELECT",
                "options": options,
            }
        )
    return {
        "node": {
            "fields": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }


@pytest.mark.asyncio
async def test_get_agent_field_state_reports_missing_field():
    manager = _agent_manager(("bot1", "Bot One"))

    async def graphql(query: str, variables: dict) -> dict:
        assert "fields(" in query
        return _fields_payload(None)

    manager._graphql = graphql  # type: ignore[method-assign]
    state = await manager.get_agent_field_state()

    assert state["exists"] is False
    assert state["options"] == []
    assert state["missing"] == [{"name": "⚙bot1", "description": "Bot One"}]


@pytest.mark.asyncio
async def test_get_agent_field_state_lists_registered_and_missing():
    manager = _agent_manager(("bot1", "Bot One"), ("bot2", "Bot Two"))

    async def graphql(query: str, variables: dict) -> dict:
        return _fields_payload(
            [{"id": "o1", "name": "⚙bot1", "description": "Bot One", "color": "GRAY"}]
        )

    manager._graphql = graphql  # type: ignore[method-assign]
    state = await manager.get_agent_field_state()

    assert state["exists"] is True
    assert state["options"] == [{"name": "⚙bot1", "description": "Bot One"}]
    assert state["missing"] == [{"name": "⚙bot2", "description": "Bot Two"}]


@pytest.mark.asyncio
async def test_sync_agent_field_creates_field_with_options():
    manager = _agent_manager(("bot1", "Bot One"))
    calls: list[tuple[str, dict]] = []
    created = {"done": False}

    async def graphql(query: str, variables: dict) -> dict:
        calls.append((query, variables))
        if "createProjectV2Field" in query:
            created["done"] = True
            return {
                "createProjectV2Field": {
                    "projectV2Field": {
                        "id": "f1",
                        "name": "Agent",
                        "dataType": "SINGLE_SELECT",
                        "options": [],
                    }
                }
            }
        return _fields_payload(
            [{"id": "o1", "name": "⚙bot1", "description": "Bot One", "color": "GRAY"}]
            if created["done"]
            else None
        )

    manager._graphql = graphql  # type: ignore[method-assign]
    state = await manager.sync_agent_field()

    create_calls = [c for c in calls if "createProjectV2Field" in c[0]]
    assert len(create_calls) == 1
    assert create_calls[0][1]["options"] == [
        {"name": "⚙bot1", "description": "Bot One", "color": "GRAY"}
    ]
    assert state["exists"] is True
    assert state["missing"] == []


@pytest.mark.asyncio
async def test_sync_agent_field_adds_missing_option_preserving_existing():
    manager = _agent_manager(("bot1", "Bot One"), ("bot2", "Bot Two"))
    submitted: dict[str, Any] = {}
    added = {"done": False}

    async def graphql(query: str, variables: dict) -> dict:
        if "updateProjectV2Field" in query:
            submitted["options"] = variables["options"]
            added["done"] = True
            return {
                "updateProjectV2Field": {"projectV2Field": {"id": "agent-field-id"}}
            }
        if "ProjectV2SingleSelectField" in query and "fields(" not in query:
            return {
                "node": {
                    "options": [
                        {
                            "id": "o1",
                            "name": "⚙bot1",
                            "description": "Bot One",
                            "color": "GRAY",
                        }
                    ]
                }
            }
        options = [
            {"id": "o1", "name": "⚙bot1", "description": "Bot One", "color": "GRAY"}
        ]
        if added["done"]:
            options.append(
                {"id": "o2", "name": "⚙bot2", "description": "Bot Two", "color": "GRAY"}
            )
        return _fields_payload(options)

    manager._graphql = graphql  # type: ignore[method-assign]
    state = await manager.sync_agent_field()

    # Existing option resubmitted WITH its id (preserves assignments); the new
    # one is appended without an id.
    assert submitted["options"] == [
        {"id": "o1", "name": "⚙bot1", "description": "Bot One", "color": "GRAY"},
        {"name": "⚙bot2", "description": "Bot Two", "color": "GRAY"},
    ]
    assert state["exists"] is True
    assert state["missing"] == []


def _status_comment(reason: str) -> str:
    return render_workflow_status_comment(
        body="status",
        payload=workflow_status_fields(reason=reason, person_id="aiko", run_id="run-1"),
    )


@pytest.mark.asyncio
async def test_ready_ticket_is_selected_when_my_comment_predates_the_assignment():
    """Reproduces issue #392.

    The member commented as the issue's author days before a human assigned
    them. That earlier comment says nothing about the assigned work, so it must
    not suppress the run.
    """
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": "I filed this and refined the body.",
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"
    # The agent still sees the full history even though it was excluded above.
    assert [comment.content for comment in task.comments] == [
        "I filed this and refined the body."
    ]


@pytest.mark.asyncio
async def test_ready_ticket_is_skipped_when_my_comment_follows_the_assignment():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": "Done, opened the PR.",
                    "created_at": "2026-01-06T00:00:00Z",
                }
            ],
        ),
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_reassignment_makes_an_earlier_finished_ticket_actionable_again():
    """Re-assigning is how a human asks for the work to resume."""
    finished_comment = {
        "user": "aiko-gh",
        "body": "Done, opened the PR.",
        "created_at": "2026-01-06T00:00:00Z",
    }
    responses = _comments(1, [finished_comment])

    before = _Manager(
        items=[
            _item(
                number=1,
                status="In Progress",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=responses,
    )
    assert await before.first_task() is None

    after = _Manager(
        items=[
            _item(
                number=1,
                status="In Progress",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-07T00:00:00Z",
            )
        ],
        responses=responses,
    )

    task = await after.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_assignee_assignment_time_comes_from_the_assigned_event():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assigned_events=[
                    {"login": "aiko-gh", "created_at": "2026-01-05T00:00:00Z"}
                ],
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": "Reviewed this for someone else.",
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_assigned_event_for_another_user_does_not_date_my_assignment():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assigned_events=[
                    {"login": "someone-else", "created_at": "2026-01-05T00:00:00Z"}
                ],
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": "Done.",
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        ),
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_working_ticket_with_only_pre_assignment_comments_is_working_lane():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="In Progress",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "reviewer",
                    "body": "Old discussion",
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_failed_status_comment_before_the_assignment_does_not_suppress():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": _status_comment("failed"),
                    "created_at": "2026-01-03T00:00:00Z",
                }
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_unknown_assignment_time_keeps_every_comment():
    """Without an assignment time nothing can be excluded, so the brake holds."""
    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "aiko-gh", "body": "Done"}]),
    )

    assert await manager.first_task() is None


@pytest.mark.asyncio
async def test_mention_after_the_assignment_overrides_my_own_last_comment():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "human",
                    "body": "⚙aiko please take another look",
                    "created_at": "2026-01-06T00:00:00Z",
                },
                {
                    "user": "aiko-gh",
                    "body": "Looking now.",
                    "created_at": "2026-01-07T00:00:00Z",
                },
                {
                    "user": "human",
                    "body": "⚙aiko one more thing",
                    "created_at": "2026-01-08T00:00:00Z",
                },
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_mention_on_second_comment_page_reopens_the_ticket():
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="Todo",
                assignee=None,
                agent="⚙aiko",
                agent_updated_at="2026-01-05T00:00:00Z",
            )
        ]
    )
    endpoint = "/repos/GuildBotics/repo/issues/1/comments"
    manager.client_stub.response_sequences[endpoint] = [
        [
            {
                "user": {"login": "human"},
                "body": f"discussion {index}",
                "created_at": "2026-01-06T00:00:00Z",
            }
            for index in range(99)
        ]
        + [
            {
                "user": {"login": "aiko-gh"},
                "body": "Done.",
                "created_at": "2026-01-07T00:00:00Z",
            }
        ],
        [
            {
                "user": {"login": "human"},
                "body": "⚙aiko one more thing",
                "created_at": "2026-01-08T00:00:00Z",
            }
        ],
    ]

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"
    assert task.comments[-1].content == "⚙aiko one more thing"
    assert manager.client_stub.gets == [
        (endpoint, {"params": {"per_page": 100, "page": 1}}),
        (endpoint, {"params": {"per_page": 100, "page": 2}}),
    ]


@pytest.mark.asyncio
async def test_latest_assignment_wins_when_both_paths_assign_this_member():
    """Assignee and Agent field are both valid ways to ask; the newest asks."""
    manager = _Manager(
        items=[
            _item(
                number=1,
                status="In Progress",
                assigned_events=[
                    {"login": "aiko-gh", "created_at": "2026-01-05T00:00:00Z"}
                ],
                agent="⚙aiko",
                agent_updated_at="2026-01-07T00:00:00Z",
            )
        ],
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-gh",
                    "body": "Done, opened the PR.",
                    "created_at": "2026-01-06T00:00:00Z",
                }
            ],
        ),
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "working_lane"


def _search_item(number: int = 2, repo: str = "repo") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/GuildBotics/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/GuildBotics/{repo}",
        "updated_at": f"2026-01-0{number}T00:00:00Z",
    }


def _pull_request_node(
    *,
    number: int = 2,
    author: str = "aiko-gh",
    head: str = "head-2",
    reviews: list[dict[str, Any]] | None = None,
    comments: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
    requested: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"PR{number}",
        "number": number,
        "url": f"https://github.com/GuildBotics/repo/pull/{number}",
        "title": f"pr {number}",
        "body": "body",
        "createdAt": "2026-01-01T00:00:00Z",
        "headRefOid": head,
        "author": {"login": author},
        "reviewRequests": {
            "nodes": [
                {"requestedReviewer": {"login": login}} for login in requested or []
            ]
        },
        "reviews": {"nodes": reviews or []},
        "comments": {"nodes": comments or []},
        "reviewThreads": {"nodes": threads or []},
    }


def _review(
    author: str,
    *,
    commit: str,
    submitted_at: str = "2026-01-01T01:00:00Z",
    state: str = "COMMENTED",
    body: str = "",
    reply: bool = False,
) -> dict[str, Any]:
    return {
        "author": {"login": author},
        "state": state,
        "body": body,
        "submittedAt": submitted_at,
        "commit": {"oid": commit},
        "comments": {"nodes": [{"replyTo": {"id": "root"} if reply else None}]},
    }


def _thread(last_author: str, *participants: str, resolved: bool = False) -> dict:
    return {
        "isResolved": resolved,
        "participants": {
            "nodes": [
                {"author": {"login": login}} for login in (*participants, last_author)
            ]
        },
        "latest": {
            "nodes": [{"author": {"login": last_author}, "reactions": {"nodes": []}}]
        },
    }


def _patrol_manager(node: dict[str, Any], items: list[dict] | None = None) -> _Manager:
    manager = _Manager(items=items or [])
    manager.search_items = [_search_item(node["number"])]
    manager.pull_request_nodes = {node["number"]: node}
    return manager


@pytest.mark.asyncio
async def test_own_pr_with_unanswered_review_thread_is_feedback_work():
    manager = _patrol_manager(_pull_request_node(threads=[_thread("reviewer")]))

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "pull_request_feedback"
    assert task.pull_request_url == "https://github.com/GuildBotics/repo/pull/2"
    assert task.url == task.pull_request_url
    assert task.number == 2
    assert task.repository == "repo"
    assert task.status == Task.IN_PROGRESS
    assert task.assignee == "aiko"


@pytest.mark.asyncio
async def test_pull_request_work_precedes_the_ready_lane():
    manager = _patrol_manager(
        _pull_request_node(threads=[_thread("reviewer")]),
        items=[_item(number=1, status="Todo")],
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "pull_request_feedback"


@pytest.mark.asyncio
async def test_task_candidates_include_all_prs_then_ready_and_working_issues():
    manager = _Manager(
        items=[
            _item(number=1, status="In Progress"),
            _item(number=2, status="Todo"),
        ]
    )
    manager.search_items = [_search_item(4), _search_item(3)]
    manager.pull_request_nodes = {
        3: _pull_request_node(number=3, threads=[_thread("reviewer")]),
        4: _pull_request_node(number=4, threads=[_thread("reviewer")]),
    }

    candidates = await manager.get_task_candidates()

    assert [(task.number, task.trigger_reason) for task in candidates] == [
        (4, "pull_request_feedback"),
        (3, "pull_request_feedback"),
        (2, "ready_lane"),
        (1, "working_lane"),
    ]


@pytest.mark.asyncio
async def test_refresh_pull_request_skips_closed_or_draft_candidate():
    node = _pull_request_node(threads=[_thread("reviewer")])
    manager = _patrol_manager(node)
    candidate = (await manager.get_task_candidates())[0]

    node["state"] = "CLOSED"
    assert await manager.refresh_task(candidate) is None

    node["state"] = "OPEN"
    node["isDraft"] = True
    assert await manager.refresh_task(candidate) is None


@pytest.mark.asyncio
async def test_refresh_issue_uses_current_comments_and_trigger_reason():
    manager = _Manager(items=[_item(number=1, status="In Progress")])
    candidate = (await manager.get_task_candidates())[0]
    endpoint = "/repos/GuildBotics/repo/issues/1/comments"
    manager.client_stub.responses[endpoint] = [
        {
            "user": {"login": "human"},
            "body": "Please update this too",
            "created_at": "2026-01-02T00:00:00Z",
        }
    ]

    refreshed = await manager.refresh_task(candidate)

    assert refreshed is not None
    assert refreshed.trigger_reason == "issue_comment"

    manager.client_stub.responses[endpoint] = [
        {
            "user": {"login": "aiko-gh"},
            "body": "Handled",
            "created_at": "2026-01-03T00:00:00Z",
        }
    ]
    assert await manager.refresh_task(candidate) is None


@pytest.mark.asyncio
async def test_ready_lane_is_selected_when_no_pull_request_needs_the_member():
    manager = _patrol_manager(
        _pull_request_node(threads=[_thread("aiko-gh", "reviewer")]),
        items=[_item(number=1, status="Todo")],
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_reviewed_pr_with_new_commits_is_review_work():
    manager = _patrol_manager(
        _pull_request_node(
            author="other", head="head-3", reviews=[_review("aiko-gh", commit="head-2")]
        )
    )

    task = await manager.first_task()

    assert task is not None
    assert task.trigger_reason == "pull_request_review"


@pytest.mark.asyncio
async def test_review_limit_is_announced_once_and_not_dispatched():
    node = _pull_request_node(
        author="other",
        head="head-4",
        reviews=[
            _review(
                "aiko-gh",
                commit=f"head-{index}",
                submitted_at=f"2026-01-0{index}T00:00:00Z",
            )
            for index in (1, 2, 3)
        ],
    )
    manager = _patrol_manager(node)

    assert await manager.first_task() is None
    assert len(manager.comments_added) == 1
    task, body = manager.comments_added[0]
    assert task.pull_request_url == "https://github.com/GuildBotics/repo/pull/2"
    status = parse_workflow_status_comment(body)
    assert status is not None and status.reason == "review_limit"
    assert (
        t("integrations.github.github_ticket_manager.review_limit_reached", count=3)
        in body
    )

    # Once the notice is on the PR, later patrols stay quiet.
    node["comments"]["nodes"].append(
        {
            "author": {"login": "aiko-gh"},
            "body": body,
            "createdAt": "2026-01-09T00:00:00Z",
        }
    )
    assert await manager.first_task() is None
    assert len(manager.comments_added) == 1


@pytest.mark.parametrize(
    ("language", "expected_phrases"),
    [
        ("en", ("request my review", "Slack", "interactive session")),
        ("ja", ("review request", "Slack", "対話セッション")),
    ],
)
def test_review_limit_notice_explains_how_to_restart(language, expected_phrases):
    set_language(language)

    message = t(
        "integrations.github.github_ticket_manager.review_limit_reached", count=3
    )

    assert all(phrase in message for phrase in expected_phrases)


@pytest.mark.asyncio
async def test_oldest_updated_pull_request_is_served_first():
    manager = _Manager(items=[])
    manager.search_items = [_search_item(3), _search_item(2)]
    manager.pull_request_nodes = {
        2: _pull_request_node(number=2, threads=[_thread("reviewer")]),
        3: _pull_request_node(number=3, threads=[_thread("reviewer")]),
    }

    task = await manager.first_task()

    assert task is not None and task.number == 3


@pytest.mark.asyncio
async def test_open_pr_keeps_its_issue_out_of_the_working_lane():
    """Feedback on a PR is followed on the PR; the issue is never re-dispatched."""
    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "human", "body": "Also do this"}]),
    )
    manager.related_pulls = [_pull()]

    assert await manager.first_task() is None
    assert manager.moved == []


@pytest.mark.asyncio
async def test_get_ticket_url_prefers_the_task_url():
    manager = _Manager(items=[])
    task = Task(
        id="PR2",
        title="pr",
        description="",
        repository="repo",
        url="https://github.com/GuildBotics/repo/pull/2",
    )

    assert (
        await manager.get_ticket_url(task, markdown=False)
        == "https://github.com/GuildBotics/repo/pull/2"
    )
    assert manager.graphql_queries == []


class _SearchClient:
    """Records search calls and answers each qualifier with its own hits."""

    def __init__(self, hits: dict[str, list[dict[str, Any]]], status: int = 200):
        self.hits = hits
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def get(self, endpoint: str, **kwargs):
        assert endpoint == "/search/issues"
        params = kwargs["params"]
        self.calls.append(params)
        qualifier = params["q"].split()[-1].split(":")[0]
        response = _Response({"items": self.hits.get(qualifier, [])}, self.status)
        response.text = "boom"
        return response


@pytest.mark.asyncio
async def test_search_covers_both_roles_and_dedupes_oldest_first():
    manager = _Manager(items=[])
    client = _SearchClient(
        {
            "author": [_search_item(3), _search_item(2)],
            "reviewed-by": [_search_item(2), _search_item(4)],
            "review-requested": [_search_item(1)],
        }
    )
    manager.client_stub = client

    items = await GitHubTicketManager._search_pull_requests(manager)

    # ``draft:false`` keeps a PR a human converted to draft out of both roles.
    assert [params["q"] for params in client.calls] == [
        "is:pr is:open draft:false user:GuildBotics author:aiko-gh",
        "is:pr is:open draft:false user:GuildBotics reviewed-by:aiko-gh",
        "is:pr is:open draft:false user:GuildBotics review-requested:aiko-gh",
    ]
    assert all(params["per_page"] == 100 for params in client.calls)
    assert [item["number"] for item in items] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_search_failure_is_not_silently_downgraded():
    manager = _Manager(items=[])
    manager.client_stub = _SearchClient({}, status=422)

    with pytest.raises(RuntimeError, match="422"):
        await GitHubTicketManager._search_pull_requests(manager)


@pytest.mark.asyncio
async def test_pull_request_load_reads_repository_from_the_search_hit():
    manager = _Manager(items=[])
    manager.pull_request_nodes = {5: _pull_request_node(number=5)}

    pull_request = await manager._load_pull_request(_search_item(5, repo="other-repo"))

    assert pull_request.repository == "other-repo"
    assert pull_request.number == 5
    assert "headRefOid" in manager.graphql_queries[0]

    with pytest.raises(RuntimeError, match="unavailable"):
        await manager._load_pull_request(_search_item(6))


@pytest.mark.asyncio
async def test_app_member_is_recognized_under_the_graphql_login():
    """The board reads GraphQL, which names an App ``<app>``; the member's
    username is the REST form ``<app>[bot]``. Both assignment paths and the
    assignment time must still find the member."""
    manager = _Manager(
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
        responses=_comments(
            1,
            [
                {
                    "user": "aiko-app[bot]",
                    "body": "Done earlier",
                    "created_at": "2026-01-04T00:00:00Z",
                }
            ],
        ),
        github_username="aiko-app[bot]",
    )

    task = await manager.first_task()

    assert task is not None
    assert task.assignee == "aiko"
    # The comment predates the assignment, so it is not this member's answer.
    assert task.trigger_reason == "ready_lane"
    assert task.comments[0].author_type == Message.ASSISTANT
    assert manager._text_mentions_me("@aiko-app please")
