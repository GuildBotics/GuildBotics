"""Bundled inspections combine pages, preserve evidence, and fail on gaps."""

import json
from types import SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import load_command_metadata, parse_command_access
from guildbotics.commands.python_command import _load_python_module
from guildbotics.runtime.code_hosting_service import (
    RepositoryReadPage,
    RepositoryReadError,
)
from guildbotics.utils.fileio import get_template_path
from guildbotics.utils.i18n_tool import t


def command(name):
    return _load_python_module(get_template_path() / f"commands/repository/{name}.py")


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def read(self, resource, repo, **kwargs):
        self.calls.append((resource, repo, kwargs))
        result = self.pages[resource].pop(0)
        if isinstance(result, Exception):
            raise result
        return RepositoryReadPage(**result)

    def context(self):
        return SimpleNamespace(get_code_hosting_service=lambda: self)


def page(*items, continuation=None):
    return {"items": list(items), "continuation": continuation}


@pytest.mark.asyncio
async def test_issue_joins_all_comments_projects_and_deduplicates_linked_prs():
    link = {"pull_request": {"repo": "other/fork", "number": 9}}
    service = Pages(
        {
            "issues": [
                page({"title": "Issue", "labels": ["bug"], "assignees": ["aiko"]})
            ],
            "issue_comments": [
                page({"id": 1}, continuation="comments-2"),
                page({"id": 2}),
            ],
            "issue_projects": [page({"project_title": "Board"})],
            "issue_timeline": [page(link, continuation="timeline-2"), page(link)],
            "pull_requests": [
                page(
                    {
                        "number": 9,
                        "html_url": "url",
                        "title": "Fix",
                        "state": "closed",
                        "merged": True,
                    }
                )
            ],
        }
    )
    result = json.loads(
        await command("issue_inspect").main(service.context(), "org/repo", "7")
    )
    assert result["comments"] == [{"id": 1}, {"id": 2}]
    assert result["labels"] == ["bug"] and result["assignees"] == ["aiko"]
    assert result["project_metadata"] == {"items": [{"project_title": "Board"}]}
    assert result["linked_pull_request_candidates"] == [
        {
            "number": 9,
            "html_url": "url",
            "title": "Fix",
            "state": "closed",
            "merged": True,
        }
    ]
    assert service.calls[-1][1:] == (
        "other/fork",
        {"identifier": "9", "parameters": {}, "continuation": ""},
    )


@pytest.mark.asyncio
async def test_pr_combines_all_feedback_and_nested_comment_pages_and_diff():
    readiness = {
        "readiness": "blocked",
        "completion_blockers": [{"code": "base_changed"}],
    }
    service = Pages(
        {
            "pull_requests": [page({"changed_files": 2, "head_repo": "fork/repo"})],
            "pull_request_readiness": [page(readiness)],
            "issue_comments": [page({"id": 1}, continuation="more"), page({"id": 2})],
            "pull_request_reviews": [
                page({"state": "COMMENTED"}, continuation="more"),
                page({"state": "APPROVED"}),
            ],
            "pull_request_threads": [
                page(
                    {
                        "id": "thread1",
                        "resolved": True,
                        "outdated": True,
                        "comments_complete": False,
                        "comments": [],
                    },
                    continuation="more",
                ),
                page(),
            ],
            "review_thread_comments": [
                page({"id": 10, "reply_to_id": None}, continuation="more"),
                page({"id": 11, "reply_to_id": 10}),
            ],
            "pull_request_files": [
                page(
                    {
                        "path": "a",
                        "patch_complete": True,
                        "commentable_lines": [{"line": 1, "side": "RIGHT"}],
                    },
                    continuation="more",
                ),
                page(
                    {
                        "path": "binary",
                        "patch_available": False,
                        "patch_complete": False,
                    }
                ),
            ],
        }
    )
    result = json.loads(
        await command("pr_inspect").main(
            service.context(), "org/repo", "7", "true", "true"
        )
    )
    assert result["checks"] == readiness
    from guildbotics.integrations.github.pull_request_patrol import (
        PULL_REQUEST_FEEDBACK_SOURCES,
    )

    assert PULL_REQUEST_FEEDBACK_SOURCES <= result.keys()
    assert result["diff_complete"] is False
    assert (
        len(result["conversation_comments"])
        == len(result["review_summaries"])
        == len(result["files"])
        == 2
    )
    thread = result["review_threads"][0]
    assert thread["resolved"] and thread["outdated"] and thread["replyable"]
    assert thread["reply_target_id"] == 10 and len(thread["comments"]) == 2
    assert all(
        c[2]["parameters"] == {"page_size": 5}
        for c in service.calls
        if c[0] == "pull_request_files"
    )
    assert all(
        c[2]["parameters"] == {"node": "thread1"}
        for c in service.calls
        if c[0] == "review_thread_comments"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("readiness", ["ready", "blocked", "not_applicable"])
async def test_checks_displays_host_result_verbatim(readiness):
    result = {
        "readiness": readiness,
        "checks_expected": True,
        "rollup": "no_checks",
        "head_sha": "head",
        "failed_logs": [{"log": "failure", "truncated": True}],
    }
    service = Pages({"pull_request_readiness": [page(result)]})
    assert (
        json.loads(
            await command("pr_checks").main(
                service.context(), "org/repo", "7", "true", "123"
            )
        )
        == result
    )
    assert service.calls[0][2]["parameters"] == {
        "failed_logs": True,
        "log_tail_bytes": 123,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource", ["issue_comments", "issue_projects", "issue_timeline"]
)
async def test_partial_failure_is_not_an_empty_or_complete_issue(resource):
    pages = {
        "issues": [page({"title": "Issue"})],
        "issue_comments": [page()],
        "issue_projects": [page()],
        "issue_timeline": [page()],
    }
    pages[resource] = [
        page({"id": 1}, continuation="next"),
        RepositoryReadError("Read denied"),
    ]
    service = Pages(pages)
    with pytest.raises(CommandError, match="Read denied"):
        await command("issue_inspect").main(service.context(), "org/repo", "7")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cycle", "size", "pages"])
async def test_aggregate_bounds_never_return_a_complete_result(monkeypatch, failure):
    from guildbotics.commands.repository import RepositoryReader

    if failure == "size":
        monkeypatch.setattr("guildbotics.commands.repository.STREAM_READ_LIMIT", 32)
    pages = (
        [page({"body": "large"}, continuation="next")] * 2
        if failure != "pages"
        else [page(continuation=str(i)) for i in range(100)]
    )
    service = Pages({"issue_comments": pages})
    with pytest.raises(CommandError) as error:
        await RepositoryReader(service.context(), "org/repo", "7").read(
            "issue_comments"
        )
    assert str(error.value) == t(
        "commands.repository.inspect.incomplete", resource="issue_comments"
    )


@pytest.mark.asyncio
async def test_github_file_limit_cannot_hide_missing_diffs():
    service = Pages(
        {
            "pull_requests": [page({"changed_files": 3001})],
            "pull_request_readiness": [page({})],
            "pull_request_files": [page({"path": "a"})],
        }
    )
    with pytest.raises(CommandError):
        await command("pr_inspect").main(
            service.context(), "org/repo", "7", include_diff="true"
        )


@pytest.mark.asyncio
async def test_empty_issue_data_is_complete():
    service = Pages(
        {
            "issues": [page({"title": "Issue"})],
            "issue_comments": [page()],
            "issue_projects": [page()],
            "issue_timeline": [page()],
        }
    )
    result = json.loads(
        await command("issue_inspect").main(service.context(), "org/repo", "7")
    )
    assert result["comments"] == result["linked_pull_request_candidates"] == []
    assert result["project_metadata"] == {"items": []}


@pytest.mark.parametrize("name", ["issue_inspect", "pr_inspect", "pr_checks"])
def test_inspections_are_read_only(name):
    path = get_template_path() / f"commands/repository/{name}.py"
    assert parse_command_access(load_command_metadata(path, "en")).read_only


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["issue_inspect", "pr_inspect", "pr_checks"])
@pytest.mark.parametrize("number", ["abc", "0", "-1", "1.2"])
async def test_command_number_errors_name_the_command_argument(name, number):
    service = Pages({})
    with pytest.raises(CommandError) as error:
        await command(name).main(service.context(), "org/repo", number)
    assert str(error.value) == t("commands.repository.inspect.number")
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["abc", "0", "-1", "65537", "100000"])
async def test_command_log_size_errors_explain_the_range(size):
    from guildbotics.runtime.code_hosting_service import MAX_LOG_TAIL_BYTES

    service = Pages({})
    with pytest.raises(CommandError) as error:
        await command("pr_checks").main(
            service.context(), "org/repo", "7", log_tail_bytes=size
        )
    assert str(error.value) == t(
        "commands.repository.inspect.log_size", maximum=MAX_LOG_TAIL_BYTES
    )
    assert service.calls == []
