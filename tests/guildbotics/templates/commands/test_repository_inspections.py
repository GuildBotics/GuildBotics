"""Bundled inspections combine pages, preserve evidence, and fail on gaps."""

import json
from types import SimpleNamespace

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import load_command_metadata, parse_command_access
from guildbotics.commands.python_command import _load_python_module
from guildbotics.runtime.port import ServiceError
from guildbotics.runtime.resources import ReadPage
from guildbotics.utils.fileio import get_template_path
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.local_code_host import (
    comment,
    local_member,
    pull_request,
    ticket,
)

TICKET_URL = "https://tickets.example.com/browse/OPS-7"
PR_URL = "https://code.example.com/org/repo/pull/7"


def command(name):
    return _load_python_module(get_template_path() / f"commands/repository/{name}.py")


class Pages:
    """The pages each resource reads, and which service was asked for each."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def service(self, name):
        pages = self

        class Service:
            async def read(self, resource, subject, **kwargs):
                pages.calls.append((name, resource, subject, kwargs))
                result = pages.pages[resource].pop(0)
                if isinstance(result, Exception):
                    raise result
                return ReadPage(**result)

        return Service()

    def context(self):
        return SimpleNamespace(
            get_code_hosting_service=lambda: self.service("code"),
            get_ticket_service=lambda: self.service("tickets"),
        )


def page(*items, continuation=None):
    return {"items": list(items), "continuation": continuation}


def _linked(url, title="Fix"):
    return {
        "url": url,
        "label": "#9",
        "title": title,
        "state": "closed",
        "merged": True,
    }


@pytest.mark.asyncio
async def test_issue_joins_all_comments_and_deduplicates_linked_prs_by_url():
    linked = _linked("https://code.example.com/org/repo/pull/9")
    other = _linked("https://code.example.com/org/repo/pull/10", "Other")
    service = Pages(
        {
            "tickets": [
                page({"title": "Ticket", "labels": ["bug"], "assignees": ["aiko"]})
            ],
            "ticket_comments": [
                page({"id": "1"}, continuation="comments-2"),
                page({"id": "2"}),
            ],
            "ticket_pull_requests": [
                page(linked, other, continuation="prs-2"),
                page({**linked, "title": "Fix, read again"}),
            ],
        }
    )
    result = json.loads(
        await command("issue_inspect").main(service.context(), TICKET_URL)
    )
    assert result["comments"] == [{"id": "1"}, {"id": "2"}]
    assert result["labels"] == ["bug"] and result["assignees"] == ["aiko"]
    assert result["linked_pull_request_candidates"] == [
        {**linked, "title": "Fix, read again"},
        other,
    ]
    # The ticket and its comments are the ticket service's; the pull requests
    # that refer to it, the code host's. Every read is of the ticket's URL.
    assert [
        (name, resource, subject) for name, resource, subject, _ in service.calls
    ] == [
        ("tickets", "tickets", TICKET_URL),
        ("tickets", "ticket_comments", TICKET_URL),
        ("tickets", "ticket_comments", TICKET_URL),
        ("code", "ticket_pull_requests", TICKET_URL),
        ("code", "ticket_pull_requests", TICKET_URL),
    ]


@pytest.mark.asyncio
async def test_issue_inspect_reads_a_local_ticket_and_the_prs_that_refer_to_it():
    member = local_member()
    url = ticket(1, title="Login", comments=[comment("someone", "it fails")])
    pr = pull_request(2, "ticket/T-1", tickets=[{"url": url, "closes": False}])
    pull_request(3, "elsewhere")

    result = json.loads(await command("issue_inspect").main(member, url))

    assert (result["url"], result["key"], result["title"]) == (url, "T-1", "Login")
    assert [c["body"] for c in result["comments"]] == ["it fails"]
    assert [pull["url"] for pull in result["linked_pull_request_candidates"]] == [pr]


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
            "pull_request_comments": [
                page({"id": "1"}, continuation="more"),
                page({"id": "2"}),
            ],
            "pull_request_reviews": [
                page({"verdict": "comment"}, continuation="more"),
                page({"verdict": "approve"}),
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
                page({"id": "10", "reply_to_id": None}, continuation="more"),
                page({"id": "11", "reply_to_id": "10"}),
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
        await command("pr_inspect").main(service.context(), PR_URL, "true", "true")
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
    assert thread["reply_target_id"] == "10" and len(thread["comments"]) == 2
    assert {(name, subject) for name, _, subject, _ in service.calls} == {
        ("code", PR_URL)
    }
    assert all(
        c[3]["parameters"] == {"page_size": 5}
        for c in service.calls
        if c[1] == "pull_request_files"
    )
    assert all(
        c[3]["parameters"] == {"node": "thread1"}
        for c in service.calls
        if c[1] == "review_thread_comments"
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
            await command("pr_checks").main(service.context(), PR_URL, "true", "123")
        )
        == result
    )
    assert service.calls[0][2] == PR_URL
    assert service.calls[0][3]["parameters"] == {
        "failed_logs": True,
        "log_tail_bytes": 123,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["ticket_comments", "ticket_pull_requests"])
async def test_partial_failure_is_not_an_empty_or_complete_issue(resource):
    pages = {
        "tickets": [page({"title": "Ticket"})],
        "ticket_comments": [page()],
        "ticket_pull_requests": [page()],
    }
    pages[resource] = [
        page({"id": "1"}, continuation="next"),
        ServiceError("Read denied"),
    ]
    service = Pages(pages)
    with pytest.raises(CommandError, match="Read denied"):
        await command("issue_inspect").main(service.context(), TICKET_URL)


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
    service = Pages({"ticket_comments": pages})
    with pytest.raises(CommandError) as error:
        await RepositoryReader(service.context(), TICKET_URL).read("ticket_comments")
    assert str(error.value) == t(
        "commands.repository.inspect.incomplete", resource="ticket_comments"
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
        await command("pr_inspect").main(service.context(), PR_URL, include_diff="true")


@pytest.mark.asyncio
async def test_empty_issue_data_is_complete():
    service = Pages(
        {
            "tickets": [page({"title": "Ticket"})],
            "ticket_comments": [page()],
            "ticket_pull_requests": [page()],
        }
    )
    result = json.loads(
        await command("issue_inspect").main(service.context(), TICKET_URL)
    )
    assert result["comments"] == result["linked_pull_request_candidates"] == []


@pytest.mark.parametrize("name", ["issue_inspect", "pr_inspect", "pr_checks"])
def test_inspections_are_read_only(name):
    path = get_template_path() / f"commands/repository/{name}.py"
    assert parse_command_access(load_command_metadata(path, "en")).read_only


@pytest.mark.asyncio
@pytest.mark.parametrize("size", ["abc", "0", "-1", "65537", "100000"])
async def test_command_log_size_errors_explain_the_range(size):
    from guildbotics.runtime.resources import MAX_LOG_TAIL_BYTES

    service = Pages({})
    with pytest.raises(CommandError) as error:
        await command("pr_checks").main(service.context(), PR_URL, log_tail_bytes=size)
    assert str(error.value) == t(
        "commands.repository.inspect.log_size", maximum=MAX_LOG_TAIL_BYTES
    )
    assert service.calls == []
