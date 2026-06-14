import logging
from typing import Any

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager


class _Response:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses: dict[str, Any]):
        self.responses = responses

    async def get(self, endpoint: str, **kwargs):
        return _Response(self.responses.get(endpoint, []))


class _Manager(GitHubTicketManager):
    def __init__(
        self,
        *,
        items: list[dict],
        responses: dict[str, Any] | None = None,
        lane_map: dict[str, str] | None = None,
        statuses: list[str] | None = None,
    ):
        person = Person(
            person_id="aiko",
            name="Aiko",
            account_info={"github_username": "aiko-gh"},
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
        self.review_threads: list[dict[str, Any]] | None = None
        self.reactions_by_comment: dict[str, list[dict[str, Any]]] = {}
        self.graphql_queries: list[str] = []
        self.graphql_error = False

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

    async def _graphql(self, query: str, variables: dict) -> dict:
        self.graphql_queries.append(query)
        if self.graphql_error:
            raise RuntimeError("GraphQL failed")
        if "PullRequestReviewComment" in query:
            return {
                "node": {
                    "reactions": {
                        "nodes": self.reactions_by_comment.get(variables["id"], []),
                        "pageInfo": {"endCursor": None, "hasNextPage": False},
                    }
                }
            }
        return {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": self.review_threads or [],
                        "pageInfo": {"endCursor": None, "hasNextPage": False},
                    }
                }
            }
        }


def _item(
    *,
    number: int,
    status: str,
    assignee: str | None = "aiko-gh",
    body: str = "",
    agent: str | None = None,
) -> dict:
    field_values = [{"field": {"name": "Status"}, "name": status}]
    if agent:
        field_values.append(
            {
                "field": {
                    "id": "agent-field",
                    "name": GitHubTicketManager.FIELD_AGENT,
                },
                "name": agent,
            }
        )
    assignees = [{"login": assignee}] if assignee else []
    return {
        "fieldValues": {"nodes": field_values},
        "content": {
            "id": f"I{number}",
            "number": number,
            "title": f"issue {number}",
            "body": body,
            "createdAt": "2026-01-01T00:00:00Z",
            "assignees": {"nodes": assignees},
            "repository": {"name": "repo", "owner": {"login": "GuildBotics"}},
        },
    }


def _comments(number: int, comments: list[dict[str, str]]) -> dict[str, Any]:
    return {
        f"/repos/GuildBotics/repo/issues/{number}/comments": [
            {
                "body": comment["body"],
                "created_at": f"2026-01-01T00:0{index}:00Z",
                "user": {"login": comment["user"]},
            }
            for index, comment in enumerate(comments)
        ]
    }


def _review_thread(
    *,
    is_resolved: bool = False,
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "isResolved": is_resolved,
        "comments": {
            "nodes": [
                _review_comment_node(index, comment)
                for index, comment in enumerate(comments)
            ]
        },
    }


def _review_comment_node(index: int, comment: dict[str, Any]) -> dict[str, Any]:
    node = {
        "id": comment.get("id", f"comment-{index}"),
        "body": comment.get("body", ""),
        "createdAt": f"2026-01-01T00:0{index}:00Z",
        "author": {"login": comment["user"]},
    }
    if "reactions" in comment:
        node["reactions"] = {"nodes": comment["reactions"]}
    return node


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

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.status == Task.READY
    assert task.assignee == "aiko"
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_custom_ready_lane_is_selected():
    manager = _Manager(
        items=[_item(number=1, status="Ready")],
        lane_map={"ready": "Ready", "done": "Completed", "working": "Doing"},
    )

    task = await manager.get_task_to_work_on()

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

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_mention_allows_unassigned_ready_ticket():
    manager = _Manager(
        items=[_item(number=1, status="Todo", assignee=None, body="Please ⚙aiko")]
    )

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.trigger_reason == "ready_lane"


@pytest.mark.asyncio
async def test_working_ticket_runs_only_when_last_issue_comment_is_not_mine():
    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "reviewer", "body": "Please update"}]),
    )

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.trigger_reason == "issue_comment"

    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses=_comments(1, [{"user": "aiko-gh", "body": "Done"}]),
    )

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_assigned_working_ticket_without_comments_is_selected():
    manager = _Manager(items=[_item(number=1, status="In Progress")])

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.trigger_reason == "working_lane"


@pytest.mark.asyncio
async def test_assigned_backlog_ticket_before_ready_lane_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Backlog")],
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_assigned_icebox_ticket_after_done_lane_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Icebox")],
        statuses=["Todo", "In Progress", "Done", "Icebox"],
    )

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_intermediate_review_lane_is_treated_as_working_without_config():
    manager = _Manager(
        items=[_item(number=1, status="In Review")],
        statuses=["Todo", "In Progress", "In Review", "Done"],
    )

    task = await manager.get_task_to_work_on()

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

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_backlog_ticket_with_unhandled_comment_is_ignored():
    manager = _Manager(
        items=[_item(number=1, status="Backlog")],
        responses=_comments(1, [{"user": "reviewer", "body": "Please update"}]),
        statuses=["Backlog", "Todo", "In Progress", "Done"],
    )

    assert await manager.get_task_to_work_on() is None


@pytest.mark.asyncio
async def test_open_pr_with_unhandled_review_triggers_task():
    manager = _Manager(items=[_item(number=1, status="In Progress")])
    manager.related_pulls = [
        {
            "url": "https://github.com/GuildBotics/repo/pull/2",
            "owner": "GuildBotics",
            "repo": "repo",
            "number": 2,
            "state": "open",
            "updated_at": "2026-01-02T00:00:00Z",
        }
    ]
    manager.review_threads = [
        _review_thread(comments=[{"user": "reviewer", "body": "Please fix"}])
    ]

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.pull_request_url == "https://github.com/GuildBotics/repo/pull/2"
    assert task.trigger_reason == "pull_request_review"


@pytest.mark.asyncio
async def test_unresolved_review_thread_with_last_reviewer_comment_is_unhandled():
    manager = _Manager(items=[])
    manager.review_threads = [
        _review_thread(comments=[{"user": "reviewer", "body": "Please fix"}])
    ]

    assert await manager._has_unhandled_pull_request_review(_pull()) is True


@pytest.mark.asyncio
async def test_resolved_review_thread_is_not_unhandled():
    manager = _Manager(items=[])
    manager.review_threads = [
        _review_thread(
            is_resolved=True,
            comments=[{"user": "reviewer", "body": "Please fix"}],
        )
    ]

    assert await manager._has_unhandled_pull_request_review(_pull()) is False


@pytest.mark.asyncio
async def test_review_thread_with_last_agent_comment_is_not_unhandled():
    manager = _Manager(items=[])
    manager.review_threads = [
        _review_thread(
            comments=[
                {"user": "reviewer", "body": "Please fix"},
                {"user": "aiko-gh", "body": "Fixed"},
            ]
        )
    ]

    assert await manager._has_unhandled_pull_request_review(_pull()) is False


@pytest.mark.asyncio
async def test_review_thread_with_agent_reaction_is_not_unhandled():
    manager = _Manager(items=[])
    manager.review_threads = [
        _review_thread(
            comments=[
                {
                    "user": "reviewer",
                    "body": "No code change needed?",
                    "reactions": [{"content": "ROCKET", "user": {"login": "aiko-gh"}}],
                }
            ]
        )
    ]

    assert await manager._has_unhandled_pull_request_review(_pull()) is False


@pytest.mark.asyncio
async def test_review_thread_reactions_are_loaded_separately_to_avoid_node_limit():
    manager = _Manager(items=[])
    manager.review_threads = [
        _review_thread(
            comments=[
                {
                    "id": "review-comment-1",
                    "user": "reviewer",
                    "body": "No code change needed?",
                }
            ]
        )
    ]
    manager.reactions_by_comment = {
        "review-comment-1": [{"content": "ROCKET", "user": {"login": "aiko-gh"}}]
    }

    assert await manager._has_unhandled_pull_request_review(_pull()) is False

    review_thread_query = manager.graphql_queries[0]
    assert "reviewThreads(first: 50" in review_thread_query
    assert "comments(last: 1)" in review_thread_query
    assert "reactions(first:" not in review_thread_query
    assert any("PullRequestReviewComment" in query for query in manager.graphql_queries)


@pytest.mark.asyncio
async def test_review_thread_query_failure_is_not_silently_downgraded():
    manager = _Manager(items=[])
    manager.graphql_error = True

    with pytest.raises(RuntimeError, match="GraphQL failed"):
        await manager._has_unhandled_pull_request_review(_pull())


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

    assert await manager.get_task_to_work_on() is None
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

    assert await manager.get_task_to_work_on() is None
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
