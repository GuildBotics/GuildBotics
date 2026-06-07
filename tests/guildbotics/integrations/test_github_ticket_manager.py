import logging
from typing import Any

import pytest

from guildbotics.entities.task import Task
from guildbotics.entities.team import Person, Project, Repository, Team
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
                repositories=[Repository(name="repo", is_default=True)],
                services=services,
            ),
            members=[person],
        )
        super().__init__(logging.getLogger("test"), person, team)
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

    async def move_ticket(self, task: Task, new_status: str) -> None:
        self.moved.append((task, new_status))

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
async def test_output_pull_request_url_is_used_as_fallback():
    manager = _Manager(
        items=[_item(number=1, status="In Progress")],
        responses={
            **_comments(
                1,
                [
                    {
                        "user": "aiko-gh",
                        "body": "Output: [PR](https://github.com/GuildBotics/repo/pull/2)",
                    }
                ],
            ),
            "/repos/GuildBotics/repo/pulls/2": {
                "html_url": "https://github.com/GuildBotics/repo/pull/2",
                "state": "open",
                "merged_at": None,
                "updated_at": "2026-01-02T00:00:00Z",
            },
        },
    )
    manager.review_threads = [
        _review_thread(comments=[{"user": "reviewer", "body": "Please fix"}])
    ]

    task = await manager.get_task_to_work_on()

    assert task is not None
    assert task.pull_request_url == "https://github.com/GuildBotics/repo/pull/2"


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
