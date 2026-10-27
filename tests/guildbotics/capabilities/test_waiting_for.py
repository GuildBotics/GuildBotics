"""A declared recipient must actually receive the request through the patrol."""

import importlib

import pytest

from guildbotics.capabilities.errors import MemberCapabilityError
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.waiting_for import validate_waiting_for
from guildbotics.capabilities.work_stops import completed_stop
from guildbotics.entities.task import Task
from guildbotics.entities.team import Person
from guildbotics.integrations.local import store
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.workflow_invocation import TICKET_WORKFLOW_COMMAND
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.local_code_host import (
    comment,
    item,
    local_member,
    local_team,
    pull_request,
    remote_with,
    ticket,
    write,
)
from tests.guildbotics.member_services import MemberServices


class Context(MemberServices):
    def __init__(self, person, team):
        member = local_member(person)
        super().__init__(person, team, member.code, member.tickets)

    def clone_for(self, person):
        return Context(person, self.team)


@pytest.fixture
def context():
    members = [
        Person(
            person_id=name,
            name=name,
            person_type="agent",
            is_active=True,
            routine_commands=[TICKET_WORKFLOW_COMMAND],
        )
        for name in ("requester", "recipient", "third")
    ]
    team = local_team(members[0])
    team.members = members
    return Context(members[0], team)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["asking", "blocked"])
@pytest.mark.parametrize("role", ["author", "reviewer", "assignee", "mention"])
async def test_waiting_request_is_dispatched_recorded_and_not_announced(
    context, monkeypatch, status, role
):
    recipient = context.clone_for(context.team.members[1])
    if role in {"author", "reviewer"}:
        remote_with("feature")
        url = pull_request(
            1,
            "feature",
            author="recipient" if role == "author" else "requester",
            reviewers=["recipient"] if role == "reviewer" else [],
        )
        if role == "author":
            await context.code.review(
                url,
                "Please fix this.",
                "request_changes",
                store.git("acme/team/demo", "rev-parse", "feature"),
            )
        else:
            await context.code.comment(url, "Please review this.")
        tasks = await recipient.code.pull_request_candidates()
    else:
        url = ticket(
            assignees=["recipient"] if role == "assignee" else [], lane="in_progress"
        )
        await context.tickets.comment(
            url,
            "Please answer."
            if role == "assignee"
            else "@local-recipient Please answer.",
        )
        tasks = await recipient.tickets.candidates()
    assert [task.pull_request_url or task.url for task in tasks] == [url]
    recorded = []
    monkeypatch.setattr(
        "guildbotics.capabilities.work_stops.record_correlated_event",
        lambda **kw: recorded.append(kw),
    )
    cli = importlib.import_module("guildbotics.cli.member")
    monkeypatch.setattr(cli, "_resolve", lambda person: (context, None))
    runs = RunStore()
    runs.append_evidence(
        "run-1",
        "pr_review" if role in {"author", "reviewer"} else "issue_comment",
        {"url": url, "target": {"url": url}},
    )
    result = await cli._task_complete(
        "requester",
        "run-1",
        Work.of_ticket(url),
        status,
        "Waiting.",
        ("recipient", "recipient"),
    )
    assert result["waiting_for"] == ["recipient"]
    assert (await cli._task_status("run-1"))["waiting_for"] == ["recipient"]
    assert runs.status("run-1").waiting_for == ["recipient"]
    task = Task(
        url=url,
        title="Work",
        description="",
        pull_request_url=url if role in {"author", "reviewer"} else None,
    )
    assert completed_stop(task, "run-1", context.team) is None
    assert recorded == [
        {
            "event_type": "work_stop.suppressed",
            "person_id": "requester",
            "payload": {
                "run_id": "run-1",
                "reason": status,
                "url": url,
                "waiting_for": ["recipient"],
            },
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "self",
        "inactive",
        "no_routine",
        "unrelated",
        "done",
        "answered_mention",
        "addressed_elsewhere",
        "draft",
        "closed",
        "pr_mention",
    ],
)
async def test_unreachable_recipients_are_rejected(context, case):
    url = ticket(
        assignees=["recipient"], lane="in_progress", comments=[comment("requester")]
    )
    waiting = "recipient"
    status = "asking"
    work = Work.of_ticket(url)
    if case == "unknown":
        waiting = "missing"
    if case == "self":
        waiting = "requester"
    if case == "inactive":
        context.team.members[1].is_active = False
    if case == "no_routine":
        context.team.members[1].routine_commands = []
    if case == "unrelated":
        waiting = "third"
    if case == "done":
        status = "done"
    if case == "manual":
        work = Work("manual", "command")
    if case == "answered_mention":
        work = Work.of_ticket(
            ticket(
                2,
                comments=[
                    comment("requester", "@recipient"),
                    comment("recipient", "Answered."),
                ],
            )
        )
    if case == "addressed_elsewhere":
        work = Work.of_ticket(
            ticket(
                2,
                assignees=["recipient"],
                lane="in_progress",
                comments=[
                    comment("recipient", "Waiting."),
                    comment("requester", "@third Please answer."),
                ],
            )
        )
    if case in {"draft", "closed", "pr_mention"}:
        remote_with("feature")
        work = Work.of_ticket(
            pull_request(
                1,
                "feature",
                author="recipient" if case != "pr_mention" else "third",
                draft=case == "draft",
                state="closed" if case == "closed" else "open",
                comments=[comment("requester", "@recipient Please answer.")],
            )
        )
    with pytest.raises(MemberCapabilityError):
        await validate_waiting_for(
            context,
            work,
            status,
            (waiting,),
            [
                {
                    "evidence_type": "issue_comment",
                    "payload": {"target": {"url": work.identity}},
                }
            ],
        )


@pytest.mark.asyncio
async def test_the_author_can_wait_for_an_existing_reviewers_answer(context):
    remote_with("feature")
    url = pull_request(1, "feature", author="requester")
    other = context.clone_for(context.team.members[1])
    head = store.git("acme/team/demo", "rev-parse", "feature")
    await other.code.review(url, "A finding", "request_changes", head)
    await context.code.comment(url, "Could you clarify?")
    assert await validate_waiting_for(
        context,
        Work.of_ticket(url),
        "asking",
        ("recipient",),
        [{"evidence_type": "pr_comment", "payload": {"target": {"url": url}}}],
    ) == ["recipient"]
    [task] = await other.code.pull_request_candidates()
    assert task.pull_request_url == url


@pytest.mark.asyncio
async def test_no_recipients_keeps_the_human_wait_default(context):
    assert await validate_waiting_for(context, Work("manual", "cmd"), "done", ()) == []


@pytest.mark.asyncio
async def test_ticket_run_can_wait_on_its_pr_without_a_resumed_marker(
    context, monkeypatch
):
    remote_with("feature")
    source = ticket()
    url = pull_request(1, "feature", author="requester", reviewers=["recipient"])
    await context.code.comment(url, "Please clarify.")
    runs = RunStore()
    runs.append_evidence("run-1", "pr_comment", {"target": {"url": url}})
    cli = importlib.import_module("guildbotics.cli.member")
    monkeypatch.setattr(cli, "_resolve", lambda person: (context, None))
    result = await cli._task_complete(
        "requester",
        "run-1",
        Work.of_ticket(source),
        "asking",
        "Waiting.",
        ("recipient",),
    )
    assert result["waiting_for"] == ["recipient"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pr_create", "checks_resumed", "git_publish"])
async def test_only_comment_targets_authorize_a_wait(context, kind):
    remote_with("feature")
    url = pull_request(
        1, "feature", author="recipient", comments=[comment("requester")]
    )
    evidence = [{"evidence_type": kind, "payload": {"target": {"url": url}}}]
    with pytest.raises(MemberCapabilityError):
        await validate_waiting_for(
            context, Work.of_ticket(url), "asking", ("recipient",), evidence
        )


@pytest.mark.asyncio
async def test_each_recipient_can_receive_a_different_commented_item(context):
    remote_with("feature")
    pr = pull_request(1, "feature", author="recipient", comments=[comment("requester")])
    source = ticket(
        assignees=["third"], lane="in_progress", comments=[comment("requester")]
    )
    evidence = [
        {"evidence_type": "issue_comment", "payload": {"target": {"url": source}}},
        {"evidence_type": "pr_comment", "payload": {"target": {"url": pr}}},
    ]
    assert await validate_waiting_for(
        context,
        Work.of_ticket("unrelated-entry"),
        "asking",
        ("recipient", "third"),
        evidence,
    ) == ["recipient", "third"]


@pytest.mark.asyncio
async def test_the_run_item_does_not_authorize_a_wait_without_a_comment_on_it(context):
    source = ticket(
        assignees=["recipient"], lane="in_progress", comments=[comment("requester")]
    )
    elsewhere = ticket(2, assignees=["third"], lane="in_progress")
    with pytest.raises(MemberCapabilityError):
        await validate_waiting_for(
            context,
            Work.of_ticket(source),
            "asking",
            ("recipient",),
            [
                {
                    "evidence_type": "issue_comment",
                    "payload": {"target": {"url": elsewhere}},
                }
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["asking", "blocked"])
async def test_cli_announces_the_owner_notification_when_no_recipient_is_named(
    context, monkeypatch, status
):
    url = ticket()
    runs = RunStore()
    runs.append_evidence("run-1", "issue_comment", {"target": {"url": url}})
    cli = importlib.import_module("guildbotics.cli.member")
    monkeypatch.setattr(cli, "_resolve", lambda person: (context, None))
    result = await cli._task_complete(
        "requester", "run-1", Work.of_ticket(url), status, "Waiting."
    )
    assert result["notification"] == t("capabilities.task_runs.owner_notified")


@pytest.mark.asyncio
async def test_human_recipients_need_no_routine_or_service_identity(context):
    person = context.team.members[1]
    person.person_type = "human"
    person.routine_commands = []
    person.is_active = False
    assert await validate_waiting_for(
        context, Work.of_ticket("source"), "asking", ("recipient",)
    ) == ["recipient"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["request_changes", "approve", "comment"])
async def test_reviewer_finishes_done_without_a_stop_notice(
    context, monkeypatch, verdict
):
    remote_with("feature")
    url = pull_request(1, "feature", author="recipient", reviewers=["requester"])
    head = store.git("acme/team/demo", "rev-parse", "feature")
    await context.code.review(url, "Review verdict.", verdict, head)
    runs = RunStore()
    runs.append_evidence("run-1", "pr_review", {"target": {"url": url}})
    cli = importlib.import_module("guildbotics.cli.member")
    monkeypatch.setattr(cli, "_resolve", lambda person: (context, None))
    result = await cli._task_complete(
        "requester", "run-1", Work.of_ticket(url), "done", "Reviewed."
    )
    assert result["status"] == "done"
    assert (
        completed_stop(
            Task(url=url, title="PR", description="", pull_request_url=url),
            "run-1",
            context.team,
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work",
    [
        Work.of_ticket("local://tickets/acme/team/demo-tickets/T-99"),
        Work("chat", "source-thread"),
        Work("manual", "source-command"),
    ],
)
async def test_resumed_pr_uses_its_host_recorded_target_without_replacing_the_source(
    context, monkeypatch, work
):
    remote_with("feature")
    url = pull_request(1, "feature", author="requester", reviewers=["recipient"])
    await context.code.comment(url, "Please clarify.")
    runs = RunStore()
    runs.append_evidence(
        "run-1",
        RunStore.CHECKS_RESUMED_EVIDENCE,
        {"target": {"kind": "pull_request", "url": url}},
    )
    runs.append_evidence("run-1", "pr_comment", {"url": url, "target": {"url": url}})
    cli = importlib.import_module("guildbotics.cli.member")
    monkeypatch.setattr(cli, "_resolve", lambda person: (context, None))
    result = await cli._task_complete(
        "requester", "run-1", work, "asking", "Waiting.", ("recipient",)
    )
    assert result["waiting_for"] == ["recipient"]
    assert (result["subject_type"], result["subject_id"]) == (
        ("chat_thread" if work.kind == "chat" else work.kind),
        work.identity,
    )


@pytest.mark.asyncio
async def test_a_ticket_only_project_can_wait_for_its_human_assignee(context):
    context.team.project.services.pop("code_hosting_service")
    context.team.members[1].person_type = "human"
    url = ticket(
        assignees=["recipient"], lane="in_progress", comments=[comment("requester")]
    )
    recipients = await validate_waiting_for(
        context, Work.of_ticket(url), "asking", ("recipient",)
    )
    runs = RunStore()
    runs.append_evidence("run-1", "issue_comment", {"target": {"url": url}})
    runs.complete(
        "run-1",
        "asking",
        "Waiting",
        Work.of_ticket(url),
        "requester",
        waiting_for=recipients,
    )
    assert (
        completed_stop(
            Task(url=url, title="Work", description=""), "run-1", context.team
        )
        is not None
    )


@pytest.mark.parametrize("status", ["asking", "blocked"])
@pytest.mark.parametrize(
    "recipients", [[], ["human"], ["recipient", "human"], ["missing"], ["inactive"]]
)
def test_human_unknown_and_inactive_recipients_still_notify(
    context, status, recipients
):
    context.team.members += [
        Person(person_id="human", name="Human", person_type="human"),
        Person(person_id="inactive", name="Inactive", is_active=False),
    ]
    runs = RunStore()
    runs.append_evidence("run-1", "issue_comment", {"target": {"url": "ticket"}})
    runs.complete(
        "run-1",
        status,
        "Waiting",
        Work.of_ticket("ticket"),
        "requester",
        waiting_for=recipients,
    )
    assert (
        completed_stop(
            Task(url="ticket", title="Work", description=""), "run-1", context.team
        )
        is not None
    )


def test_a_diagnostics_failure_does_not_change_the_completed_stop(context, monkeypatch):
    runs = RunStore()
    runs.append_evidence("run-1", "issue_comment", {"target": {"url": "ticket"}})
    runs.complete(
        "run-1",
        "asking",
        "Waiting",
        Work.of_ticket("ticket"),
        "requester",
        waiting_for=["recipient"],
    )

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "guildbotics.capabilities.work_stops.record_correlated_event", fail
    )
    assert (
        completed_stop(
            Task(url="ticket", title="Work", description=""), "run-1", context.team
        )
        is None
    )
