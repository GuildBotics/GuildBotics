"""A run that stopped to await its PR's checks is resumed by the patrol once
they settle, on the local code host through the shared patrol."""

from __future__ import annotations

import subprocess

import pytest
import pytest_asyncio

from guildbotics.integrations import pull_request_patrol
from guildbotics.integrations.local import store
from guildbotics.runtime.port import ServiceError, WorkflowStatus
from tests.guildbotics.local_code_host import (
    REPOSITORY,
    comment,
    item,
    local_member,
    pull_request,
    remote_with,
    ticket,
    write,
)

ME = "aiko"
BRANCH = "ticket/T-1"


@pytest.fixture
def member():
    return local_member(checks="pending")


def _head() -> str:
    return store.branch_tip(REPOSITORY, BRANCH)


async def _awaiting(member, url: str, subject: str) -> None:
    """The member's run stops to await the checks of the head it pushed."""
    await member.code.comment(
        url,
        "Waiting for CI.",
        status=WorkflowStatus(
            reason="awaiting_checks",
            person_id=ME,
            run_id="run-1",
            subject=subject,
            head=_head(),
        ),
    )


async def _woken(member) -> list[tuple[str | None, str | None, str | None]]:
    candidates = await member.code.pull_request_candidates()
    refreshed = [await member.code.refresh_pull_request(task) for task in candidates]
    assert [task.model_dump() for task in candidates] == [
        task.model_dump() for task in refreshed if task is not None
    ]
    return [
        (task.url, task.pull_request_url, task.trigger_reason) for task in candidates
    ]


@pytest_asyncio.fixture
async def awaited(member):
    """The member's own PR, whose run from ticket T-1 awaits its checks."""
    remote_with(BRANCH)
    ticket_url = ticket(1)
    url = pull_request(1, BRANCH, author=ME)
    await _awaiting(member, url, ticket_url)
    return url, ticket_url


@pytest.mark.asyncio
async def test_no_work_while_the_checks_are_pending(member, awaited):
    assert await _woken(member) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["success", "failure"])
async def test_settled_checks_resume_the_member(member, awaited, conclusion):
    url, _ = awaited
    store.finish_checks(REPOSITORY, _head(), conclusion)

    assert await _woken(member) == [(url, url, "pull_request_checks")]


@pytest.mark.asyncio
async def test_a_new_head_resumes_the_work_while_its_checks_are_pending(
    member, awaited
):
    url, _ = awaited
    seed = store.bare(REPOSITORY).parent / "seed"
    git = ["git", "-c", "user.name=S", "-c", "user.email=s@example.com"]
    subprocess.run([*git, "switch", "-q", BRANCH], cwd=seed, check=True)
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", "more"], cwd=seed, check=True
    )
    subprocess.run(
        [*git, "push", "-q", str(store.bare(REPOSITORY)), BRANCH], cwd=seed, check=True
    )

    assert await _woken(member) == [(url, url, "pull_request_checks")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"state": "closed", "merged": True},
        {"state": "closed"},
        # A human took the PR into their own hands.
        {"draft": True},
    ],
)
async def test_a_pr_no_longer_open_and_ready_is_not_resumed(member, awaited, change):
    url, _ = awaited
    store.finish_checks(REPOSITORY, _head(), "success")
    write(store.locate(url, "pull_request"), {**item(url), **change})

    assert await _woken(member) == []


@pytest.mark.asyncio
async def test_only_the_members_next_notice_ends_the_wait(member, awaited):
    """A comment the member writes leaves the wait as it is; the notice the
    host leaves when the resumed run ends it."""
    url, _ = awaited
    store.finish_checks(REPOSITORY, _head(), "success")
    await member.code.comment(url, "CI passed; the PR is ready.")
    assert await _woken(member) == [(url, url, "pull_request_checks")]

    await pull_request_patrol.checks_settled(
        member.code, url, person_id=ME, run_id="run-2"
    )

    assert await _woken(member) == []


@pytest.mark.asyncio
async def test_someone_elses_wait_and_an_unanswered_statement_are_not_this_role(
    member,
):
    """Another member's notice resumes nobody here; a statement still asks for
    feedback first."""
    remote_with(BRANCH)
    theirs = pull_request(1, BRANCH, author="them")
    await _awaiting(member, theirs, theirs)
    mine = pull_request(
        2, BRANCH, author=ME, comments=[comment("reviewer", "please fix")]
    )
    store.finish_checks(REPOSITORY, _head(), "success")

    assert await _woken(member) == [(mine, mine, "pull_request_feedback")]


@pytest.mark.asyncio
async def test_a_wait_whose_checks_cannot_be_read_waits_on(
    member, awaited, monkeypatch, caplog
):
    """A failed read tells nothing of the checks: the patrol goes on with the
    other PRs and asks again next time."""
    url, _ = awaited
    other = pull_request(2, BRANCH, author=ME, comments=[comment("reviewer", "hi")])

    async def unreadable(_url, **_conditions):
        raise ServiceError("the code host is down")

    monkeypatch.setattr(member.code, "readiness", unreadable)

    assert await _woken(member) == [(other, other, "pull_request_feedback")]
    assert f"Could not read the checks of {url}" in caplog.text


@pytest.mark.asyncio
async def test_a_wait_on_a_head_that_is_gone_resumes_nothing(member, awaited):
    store.git(REPOSITORY, "update-ref", "-d", f"refs/heads/{BRANCH}")

    assert await _woken(member) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after_at", "resumed"),
    [("2000-01-01T00:00:00+00:00", True), ("2999-01-01T00:00:00+00:00", False)],
)
async def test_a_resumed_run_stopped_by_a_rate_limit_is_resumed_after_the_reset(
    member, awaited, retry_after_at, resumed
):
    url, _ = awaited
    store.finish_checks(REPOSITORY, _head(), "success")
    await member.code.comment(
        url,
        "Rate limited.",
        status=WorkflowStatus(
            reason="rate_limited",
            person_id=ME,
            run_id="run-2",
            subject=url,
            retry_after_at=retry_after_at,
        ),
    )

    assert await _woken(member) == (
        [(url, url, "pull_request_checks")] if resumed else []
    )
