"""The member's code-host work, on the local code host and board.

What holds whichever code host it is -- a human's approval, the readiness a
run is completed against, what each command reports -- is exercised here on
``local``; how GitHub carries each operation is
``tests/guildbotics/integrations/test_github_code_hosting.py``.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest

from guildbotics.capabilities import member_repository
from guildbotics.entities.task import Task
from guildbotics.integrations.local import store
from guildbotics.runtime.integration_factory import MemberCapabilityError
from tests.guildbotics.local_code_host import (
    comment,
    issue,
    item,
    local_member,
    pull_request,
)


@pytest.fixture
def member():
    return local_member()


def _remote_with(*branches: str) -> None:
    """owner/repo's remote, with ``main`` and ``branches`` one commit ahead."""
    import subprocess

    bare = store.bare("owner", "repo")
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


@pytest.mark.asyncio
async def test_issue_create_needs_a_human_and_lands_on_the_board(member):
    with pytest.raises(MemberCapabilityError, match="human instruction"):
        await member_repository.issue_create(member, "owner/repo", "T", "B", True)
    assert list(store.repositories()) == []

    result = await member_repository.issue_create(
        member, "owner/repo", "T", "B", True, human_approved=True
    )

    assert result == {
        "issue_number": 1,
        "issue_title": "T",
        "repo": "owner/repo",
        "issue_url": "local://owner/repo/issues/1",
        "labels": [],
        "project_item_id": "local://owner/repo/issues/1",
        "target": {
            "kind": "issue",
            "repo": "owner/repo",
            "number": 1,
            "title": "T",
            "html_url": "local://owner/repo/issues/1",
        },
    }
    assert item(result["issue_url"])["lane"] == Task.NEW


@pytest.mark.asyncio
async def test_issue_update_and_comment_report_what_they_changed(member):
    url = issue(1, labels=["bug"])

    with pytest.raises(MemberCapabilityError, match="human instruction"):
        await member_repository.issue_update(member, url, state="closed")
    with pytest.raises(MemberCapabilityError, match="at least one"):
        await member_repository.issue_update(member, url, add_labels=[" "])
    updated = await member_repository.issue_update(
        member,
        url,
        title="New",
        remove_labels=["bug"],
        state="closed",
        human_approved=True,
    )
    commented = await member_repository.issue_comment(member, url, "Closing.")

    assert (updated["title"], updated["labels"], updated["state_changed"]) == (
        "New",
        [],
        True,
    )
    assert commented["issue_url"] == url and commented["issue_number"] == 1
    assert commented["comment_url"] == commented["html_url"]
    assert [c["body"] for c in item(url)["comments"]] == ["Closing."]


@pytest.mark.asyncio
async def test_pull_request_work_reports_its_target(member):
    _remote_with("feature")
    created = await member_repository.pr_create(
        member, "owner/repo", "feature", "", "PR", "Body", "", "false"
    )
    url = created["pr_url"]

    with pytest.raises(MemberCapabilityError, match="needs a body or a title"):
        await member_repository.pr_update(member, url)
    with pytest.raises(MemberCapabilityError, match="Review event must be one of"):
        await member_repository.pr_review(member, url, "x", "lgtm")  # type: ignore[arg-type]
    with pytest.raises(MemberCapabilityError, match="side must be LEFT or RIGHT"):
        await member_repository.pr_review_comment(
            member, url, "x", "a", 1, "UP", None, None
        )
    results = [
        await member_repository.pr_update(member, url, title="Renamed"),
        await member_repository.pr_comment(member, url, "Hi"),
        await member_repository.pr_review(member, url, "LGTM", "approve"),
        inline := await member_repository.pr_review_comment(
            member, url, "Hmm", "a.py", 2, "RIGHT", None, None
        ),
        await member_repository.pr_reply(
            member, url, inline["review_comment_id"], "Fixed."
        ),
    ]

    assert created["base"] == "main" and created["created"] is True
    assert {result["target"]["html_url"] for result in results} == {url}
    pr = item(url)
    assert pr["title"] == "Renamed"
    assert [review["state"] for review in pr["reviews"]] == ["APPROVED"]
    assert [c["in_reply_to"] for c in pr["review_comments"]] == [
        None,
        inline["review_comment_id"],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "key"),
    [
        ("issue-comment", "comments"),
        ("pr-review-comment", "review_comments"),
        ("pr-review", "reviews"),
    ],
)
async def test_reaction_add_reacts_to_each_kind_of_target(member, target, key):
    entry = comment("reviewer", id=7, state="COMMENTED", commit_id="x")
    if key == "reviews":
        entry["submitted_at"] = entry.pop("created_at")
    url = pull_request(3, "feature", **{key: [{**entry, "in_reply_to": None}]})

    result = await member_repository.reaction_add(
        member, "owner/repo", target, 7, "eyes", 3
    )

    assert result == {"content": "eyes", "comment_id": 7}
    [reacted] = item(url)[key]
    assert reacted["reactions"] == [{"author": "aiko", "content": "eyes"}]


@pytest.mark.asyncio
async def test_artifact_download_extracts_the_pull_requests_artifact(member, tmp_path):
    url = pull_request(3, "feature")
    archive = store.artifact("owner", "repo", 3, "report")
    archive.parent.mkdir(parents=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("report.md", "details")
    archive.write_bytes(buffer.getvalue())

    result = await member_repository.artifact_download(
        member, url, " report ", tmp_path / "out"
    )

    assert (result["repo"], result["artifact_name"]) == ("owner/repo", "report")
    assert (tmp_path / "out" / "report.md").read_text(encoding="utf-8") == "details"
    with pytest.raises(MemberCapabilityError, match="was not found"):
        await member_repository.artifact_download(member, url, "logs", tmp_path)


@pytest.mark.asyncio
async def test_task_completion_revalidates_what_the_run_answers_for(member):
    """The PRs the run touched, and those of the ticket the member wrote; a PR
    of someone else's that the run did not touch is not the member's to make
    ready."""
    _remote_with("mine", "touched", "theirs")
    ticket = issue(1)
    mine = pull_request(2, "mine", author="aiko", issue=1)
    pull_request(3, "theirs", author="someone", issue=1)
    touched = pull_request(4, "touched", author="someone")
    pull_request(5, "mine", author="aiko", issue=1, state="closed")

    results = await member_repository.task_completion_readiness(
        member, ticket, [{"payload": {"pull_requests": [{"pr_url": touched}]}}]
    )

    assert [result["pr_url"] for result in results] == [touched, mine]
    assert all(result["readiness"] == "ready" for result in results)


@pytest.mark.asyncio
async def test_task_completion_refuses_a_blocked_pull_request(member):
    _remote_with("mine")
    failing = local_member(checks="failure")
    url = pull_request(2, "mine", author="aiko")

    with pytest.raises(MemberCapabilityError, match=re.escape("CI checks are failing")):
        await member_repository.task_completion_readiness(failing, url, [])
    assert await member_repository.task_completion_readiness(member, "task:1", []) == []


@pytest.mark.asyncio
async def test_open_pull_request_readiness_skips_what_is_not_open(member):
    _remote_with("feature")
    url = pull_request(2, "feature")
    pull_request(3, "feature", state="closed")
    remote = await member.code.clone_url("owner", "repo")

    assert await member_repository.open_pull_request_readiness(
        member.code, remote, "feature"
    ) == [{"pr_url": url, "readiness": "ready", "completion_blockers": []}]
