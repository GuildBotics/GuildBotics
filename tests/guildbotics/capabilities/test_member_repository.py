"""The member's code-host and ticket work, on the local code host and tickets.

What holds whichever service it is -- a human's approval, the readiness a run
is completed against, what each command reports -- is exercised here on
``local``; how GitHub carries each operation is
``tests/guildbotics/integrations/test_github_code_hosting.py``.
"""

from __future__ import annotations

import io
import re
import subprocess
import zipfile

import pytest

from guildbotics.capabilities import member_repository
from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.entities.task import Task
from guildbotics.integrations.local import store
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from tests.guildbotics.local_code_host import (
    REPOSITORY,
    TRACKER,
    comment,
    item,
    local_member,
    pull_request,
    ticket,
    tracker,
)

FOREIGN_TICKET = "https://tickets.example.com/browse/OPS-7"


@pytest.fixture
def member():
    return local_member()


def _remote_with(*branches: str, repository: str = REPOSITORY) -> None:
    """The repository's remote, with ``main`` and ``branches`` one commit
    ahead."""
    bare = store.bare(repository)
    seed = bare.parent / "seed"
    git = ["git", "-c", "user.name=S", "-c", "user.email=s@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "a").write_text("a", encoding="utf-8")
    subprocess.run([*git, "add", "a"], cwd=seed, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "a"], cwd=seed, check=True)
    for branch in branches:
        subprocess.run(
            [*git, "switch", "-q", "-c", branch, "main"], cwd=seed, check=True
        )
        (seed / branch.replace("/", "-")).write_text(branch, encoding="utf-8")
        subprocess.run([*git, "add", "."], cwd=seed, check=True)
        subprocess.run([*git, "commit", "-q", "-m", branch], cwd=seed, check=True)
    subprocess.run([*git, "switch", "-q", "main"], cwd=seed, check=True)
    subprocess.run([*git, "clone", "-q", "--bare", str(seed), str(bare)], check=True)


def _tickets_kept() -> list[dict]:
    return [kept for _, kept in store.items("ticket", TRACKER)]


@pytest.mark.asyncio
async def test_ticket_create_needs_a_human_and_lands_on_the_board(member):
    tracker()
    with pytest.raises(MemberCapabilityError, match="human instruction"):
        await member_repository.ticket_create(member, TRACKER, "T", "B", True)
    assert _tickets_kept() == []

    result = await member_repository.ticket_create(
        member, TRACKER, "T", "B", True, human_approved=True
    )

    url = f"local://tickets/{TRACKER}/T-1"
    assert result == {
        "labels": [],
        "target": {"kind": "ticket", "url": url, "title": "T", "label": "T-1"},
        "on_board": True,
    }
    assert item(url)["lane"] == Task.NEW


@pytest.mark.asyncio
async def test_ticket_create_off_the_board_says_so(member):
    tracker()

    result = await member_repository.ticket_create(
        member, TRACKER, "T", "B", False, human_approved=True
    )

    assert result["on_board"] is False
    assert item(result["target"]["url"])["lane"] is None


@pytest.mark.asyncio
async def test_a_board_failure_after_creation_says_the_ticket_was_created(
    member, monkeypatch
):
    """The ticket is there whatever the board did: the failure says so, so
    the member does not open it a second time."""
    tracker()

    async def refuse(_url):
        raise ServiceError("The board is unavailable.")

    monkeypatch.setattr(member.tickets, "add", refuse)

    url = f"local://tickets/{TRACKER}/T-1"
    with pytest.raises(
        ServiceError,
        match=re.escape(
            f"{url} was created, but it could not be put on the board: "
            "The board is unavailable."
        ),
    ):
        await member_repository.ticket_create(
            member, TRACKER, "T", "B", True, human_approved=True
        )
    assert [kept["title"] for kept in _tickets_kept()] == ["T"]


@pytest.mark.asyncio
async def test_ticket_update_and_comment_report_what_they_changed(member):
    url = ticket(1, labels=["bug"])

    with pytest.raises(MemberCapabilityError, match="human instruction"):
        await member_repository.ticket_update(member, url, state="closed")
    with pytest.raises(MemberCapabilityError, match="at least one"):
        await member_repository.ticket_update(member, url, add_labels=[" "])
    updated = await member_repository.ticket_update(
        member,
        url,
        title="New",
        remove_labels=["bug", "never-carried"],
        state="closed",
        human_approved=True,
    )
    commented = await member_repository.ticket_comment(member, url, "Closing.")

    target = {"kind": "ticket", "url": url, "title": "New", "label": "T-1"}
    assert updated == {
        "body": "",
        "state": "closed",
        "state_changed": True,
        "labels": [],
        "target": target,
    }
    assert commented == {
        "id": "comment-1",
        "url": None,
        "author": "aiko",
        "created_at": commented["created_at"],
        "target": target,
    }
    assert [c["body"] for c in item(url)["comments"]] == ["Closing."]


@pytest.mark.asyncio
async def test_removing_a_label_the_ticket_does_not_carry_changes_nothing(member):
    url = ticket(1, labels=["bug"])

    updated = await member_repository.ticket_update(
        member, url, remove_labels=["question"]
    )

    assert updated["labels"] == ["bug"]
    assert item(url)["labels"] == ["bug"]


@pytest.mark.asyncio
async def test_pull_request_work_reports_its_target(member):
    _remote_with("feature")
    created = await member_repository.pr_create(
        member, REPOSITORY, "feature", "", "PR", "Body", "", "false"
    )
    url = created["target"]["url"]
    head = store.branch_tip(REPOSITORY, "feature")

    with pytest.raises(MemberCapabilityError, match="needs a body or a title"):
        await member_repository.pr_update(member, url)
    with pytest.raises(MemberCapabilityError, match="Review verdict must be one of"):
        await member_repository.pr_review(member, url, "x", "lgtm", head)  # type: ignore[arg-type]
    with pytest.raises(MemberCapabilityError, match="side must be LEFT or RIGHT"):
        await member_repository.pr_review_comment(
            member, url, "x", head, "a", 1, "UP", None, None
        )
    results = [
        await member_repository.pr_update(member, url, title="Renamed"),
        await member_repository.pr_comment(member, url, "Hi"),
        await member_repository.pr_review(member, url, "LGTM", "approve", head),
        inline := await member_repository.pr_review_comment(
            member, url, "Hmm", head, "a.py", 2, "RIGHT", None, None
        ),
        await member_repository.pr_reply(member, url, inline["id"], "Fixed."),
    ]

    assert (created["base"], created["created"]) == ("main", True)
    assert created["target"] == {
        "kind": "pull_request",
        "url": f"local://code/{REPOSITORY}/pulls/1",
        "title": "PR",
        "label": "!1",
    }
    assert {result["target"]["url"] for result in results} == {url}
    pr = item(url)
    assert pr["title"] == "Renamed"
    assert [(r["verdict"], r["commit"]) for r in pr["reviews"]] == [("approve", head)]
    assert [c["in_reply_to"] for c in pr["review_comments"]] == [None, inline["id"]]


@pytest.mark.asyncio
async def test_a_review_on_a_head_that_moved_writes_nothing(member):
    _remote_with("feature")
    url = pull_request(1, "feature")

    with pytest.raises(ServiceError, match="head"):
        await member_repository.pr_review(member, url, "LGTM", "approve", "stale")
    with pytest.raises(ServiceError, match="head"):
        await member_repository.pr_review_comment(
            member, url, "Hmm", "stale", "a.py", 1, "RIGHT", None, None
        )

    assert (item(url)["reviews"], item(url)["review_comments"]) == ([], [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_id", "key"),
    [
        ("comment-7", "comments"),
        ("inline-7", "review_comments"),
        ("review-7", "reviews"),
    ],
)
async def test_pr_reaction_reacts_to_each_kind_of_entry(member, entry_id, key):
    entry = comment("reviewer", id=entry_id, verdict="comment", commit="x")
    url = pull_request(3, "feature", **{key: [{**entry, "in_reply_to": None}]})

    result = await member_repository.pr_reaction(member, url, entry_id, "ack")

    assert result == {"reaction": "ack", "id": entry_id}
    [reacted] = item(url)[key]
    assert reacted["reactions"] == [{"author": "aiko", "reaction": "ack"}]


@pytest.mark.asyncio
async def test_ticket_reaction_reacts_to_a_comment_of_the_ticket(member):
    url = ticket(1, comments=[comment("someone", id="comment-2")])

    result = await member_repository.ticket_reaction(
        member, url, "comment-2", "celebrate"
    )

    assert result == {"reaction": "celebrate", "id": "comment-2"}
    [reacted] = item(url)["comments"]
    assert reacted["reactions"] == [{"author": "aiko", "reaction": "celebrate"}]


@pytest.mark.asyncio
async def test_a_reaction_looks_only_in_the_item_the_url_names(member):
    """The comment is found in the ticket or pull request ``url`` names; the
    same id on another item is not it."""
    pull_request(3, "feature", comments=[comment("reviewer", id="comment-7")])
    other = pull_request(4, "other")

    with pytest.raises(ServiceNotFound, match="'comment-7' is none of"):
        await member_repository.pr_reaction(member, other, "comment-7", "ack")
    assert item(other)["comments"] == []


@pytest.mark.asyncio
async def test_a_ticket_and_a_pull_request_of_one_number_are_two_items(member):
    """Tickets and pull requests are numbered each in their own place: ticket
    T-1 and pull request !1 are both there, and a URL reaches the one it
    names."""
    ticket_url = ticket(1)
    pr_url = pull_request(1, "feature")

    await member_repository.ticket_comment(member, ticket_url, "To the ticket")
    await member_repository.pr_comment(member, pr_url, "To the pull request")

    assert [c["body"] for c in item(ticket_url)["comments"]] == ["To the ticket"]
    assert [c["body"] for c in item(pr_url)["comments"]] == ["To the pull request"]
    for resource, subject, body in (
        ("ticket_comments", ticket_url, "To the ticket"),
        ("pull_request_comments", pr_url, "To the pull request"),
    ):
        page = await member_repository.read_resource(
            member, resource, subject, parameters={}, continuation=""
        )
        assert [c["body"] for c in page["items"]] == [body]


def _artifact(sha: str, name: str, files: dict[str, str]) -> None:
    archive = store.artifact(REPOSITORY, sha, name)
    archive.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for path, content in files.items():
            bundle.writestr(path, content)
    archive.write_bytes(buffer.getvalue())


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../other-sha/report", "nested/report"])
async def test_artifact_download_reads_only_the_heads_artifacts(member, tmp_path, name):
    """An artifact is named among the head commit's own: a name that leads
    into another commit's artifacts, or anywhere else, is refused."""
    _remote_with("feature")
    url = pull_request(3, "feature")
    _artifact("other-sha", "report", {"report.md": "theirs"})

    with pytest.raises(ServiceNotFound, match="is no artifact name"):
        await member_repository.artifact_download(member, url, name, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.asyncio
async def test_artifact_download_refuses_a_path_as_the_name(member, tmp_path):
    _remote_with("feature")
    url = pull_request(3, "feature")

    with pytest.raises(ServiceNotFound, match="is no artifact name"):
        await member_repository.artifact_download(
            member, url, str(tmp_path / "anywhere"), tmp_path / "out"
        )


@pytest.mark.asyncio
async def test_artifact_download_extracts_the_heads_artifact(member, tmp_path):
    _remote_with("feature")
    url = pull_request(3, "feature")
    head = store.branch_tip(REPOSITORY, "feature")
    _artifact(head, "report", {"report.md": "details"})

    result = await member_repository.artifact_download(
        member, url, " report ", tmp_path / "out"
    )

    assert (result["name"], result["id"]) == ("report", f"{head}/report")
    assert (tmp_path / "out" / "report.md").read_text(encoding="utf-8") == "details"
    with pytest.raises(ServiceNotFound, match="has no artifact 'logs'"):
        await member_repository.artifact_download(member, url, "logs", tmp_path)


@pytest.mark.asyncio
async def test_task_completion_revalidates_what_the_run_answers_for(member):
    """The PRs the run touched, and those that refer to the member's ticket
    and the member wrote; a PR of someone else's that the run did not touch is
    not the member's to make ready."""
    _remote_with("mine", "touched", "theirs")
    ticket_url = ticket(1)
    mine = pull_request(2, "mine", author="aiko", ticket=ticket_url)
    pull_request(3, "theirs", author="someone", ticket=ticket_url)
    touched = pull_request(4, "touched", author="someone")
    pull_request(5, "mine", author="aiko", ticket=ticket_url, state="closed")

    results = await member_repository.task_completion_readiness(
        member, ticket_url, [{"payload": {"pull_requests": [{"url": touched}]}}]
    )

    assert [result["target"]["url"] for result in results] == [mine, touched]
    assert all(result["readiness"] == "ready" for result in results)


@pytest.mark.asyncio
async def test_task_completion_takes_the_pull_requests_the_run_wrote_to(member):
    """A write's target names a pull request; a ticket target is no pull
    request to make ready."""
    _remote_with("touched")
    ticket_url = ticket(1)
    touched = pull_request(2, "touched", author="someone")

    results = await member_repository.task_completion_readiness(
        member,
        ticket_url,
        [
            {"payload": {"target": {"kind": "pull_request", "url": touched}}},
            {"payload": {"target": {"kind": "ticket", "url": ticket_url}}},
        ],
    )

    assert [result["target"]["url"] for result in results] == [touched]


@pytest.mark.asyncio
async def test_task_completion_of_a_pull_request_revalidates_it(member):
    """A run whose ticket is a pull request answers for it, when the member
    wrote it."""
    _remote_with("mine")
    url = pull_request(2, "mine", author="aiko")

    results = await member_repository.task_completion_readiness(member, url, [])

    assert [result["target"]["url"] for result in results] == [url]


@pytest.mark.asyncio
async def test_task_completion_of_another_services_ticket_has_no_pull_requests(
    member,
):
    """The code host cannot tell which of its pull requests refer to a
    ticket kept elsewhere: that is none, not a failure."""
    assert (
        await member_repository.task_completion_readiness(member, FOREIGN_TICKET, [])
        == []
    )


@pytest.mark.asyncio
async def test_task_completion_refuses_a_blocked_pull_request():
    _remote_with("mine")
    failing = local_member(checks="failure")
    url = pull_request(2, "mine", author="aiko")

    with pytest.raises(MemberCapabilityError, match=re.escape("CI checks are failing")):
        await member_repository.task_completion_readiness(failing, url, [])


@pytest.mark.asyncio
async def test_open_pull_request_readiness_skips_what_is_not_open(member):
    _remote_with("feature")
    url = pull_request(2, "feature")
    pull_request(3, "feature", state="closed")

    assert await member_repository.open_pull_request_readiness(
        member.code, REPOSITORY, "feature"
    ) == [{"url": url, "readiness": "ready", "completion_blockers": []}]


@pytest.mark.asyncio
async def test_a_remote_git_cannot_read_is_a_failure_not_an_answer(member):
    """What the code host says of its remote comes from git, or not at all."""
    with pytest.raises(ServiceNotFound, match="has no remote repository"):
        await member.code.default_branch(REPOSITORY)
    _remote_with()
    bare = store.bare(REPOSITORY)
    sha = store.git(REPOSITORY, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "update-ref", "--no-deref", "HEAD", sha], cwd=bare, check=True
    )

    with pytest.raises(ServiceError, match="git symbolic-ref failed"):
        await member.code.default_branch(REPOSITORY)


@pytest.mark.asyncio
async def test_a_pull_request_from_a_deleted_fork_is_still_read_and_patrolled(member):
    """A fork that is gone has no head: one such pull request is no failure of
    the whole patrol, and still reads as itself."""
    _remote_with()
    url = pull_request(
        5,
        "feature",
        author="them",
        head_repository="contributor/team/repo",
        reviewers=["aiko"],
    )

    [task] = await member.code.pull_request_candidates()
    page = await member_repository.read_resource(
        member, "pull_requests", url, parameters={}, continuation=""
    )

    assert (task.pull_request_url, task.trigger_reason) == (url, "pull_request_review")
    assert page["items"][0]["head_sha"] == ""
    with pytest.raises(ServiceError, match="has no head commit"):
        await member.code.readiness(url)
