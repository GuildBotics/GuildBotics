"""A member announces in chat that its ticket or pull request work waits for
a person (``guildbotics.capabilities.work_stops``)."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

import guildbotics
from guildbotics.capabilities import work_stops
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.work_stops import (
    WorkStop,
    comment_url_of,
    notify_work_stop,
)
from guildbotics.entities.task import Task
from guildbotics.entities.task_run import TASK_RUN_COMPLETIONS
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.local.identity import username
from guildbotics.integrations.local.chat import LocalChatService, channel_path
from guildbotics.runtime.member_invocation import Work
from guildbotics.runtime.port import (
    ServiceError,
    WorkflowStatus,
    WorkflowStatusReason,
)
from guildbotics.utils.i18n_tool import t
from tests.guildbotics.local_chat import chat_team, lines, say

TICKET = "local://tickets/acme/team/demo-tickets/T-1"
PULL = "local://code/acme/team/demo/pulls/2"
COMMENT = f"{TICKET}#comment-3"


def _evidence(evidence_type: str, url: str, target: str) -> dict:
    payload: dict = {"id": "comment-1", "target": {"url": target}}
    if url:
        payload["url"] = url
    return {"evidence_type": evidence_type, "payload": payload}


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ([], ""),
        # The latest comment on the item, the one that says why it stopped.
        (
            [
                _evidence("issue_comment", f"{TICKET}#comment-1", TICKET),
                _evidence("pr_comment", f"{PULL}#comment-2", PULL),
                _evidence("issue_comment", COMMENT, TICKET),
                _evidence("pr_reply", f"{PULL}#comment-4", PULL),
            ],
            COMMENT,
        ),
        # Every kind of comment counts, reviews and replies included.
        ([_evidence("pr_review", f"{TICKET}#review-1", TICKET)], f"{TICKET}#review-1"),
        # None on the item: the latest comment the run wrote anywhere.
        (
            [
                _evidence("pr_comment", f"{PULL}#comment-2", PULL),
                _evidence("pr_review_comment", f"{PULL}#comment-5", PULL),
            ],
            f"{PULL}#comment-5",
        ),
        # A write that is no comment, and a comment with no address, are no link.
        (
            [
                _evidence("pr_create", PULL, PULL),
                _evidence("reaction_add", f"{TICKET}#comment-1", TICKET),
                _evidence("issue_comment", "", TICKET),
            ],
            "",
        ),
    ],
)
def test_comment_url_of_names_the_members_latest_comment(evidence, expected):
    assert comment_url_of(evidence, TICKET) == expected


def _person(*channels: dict) -> Person:
    return Person(
        person_id="aiko",
        name="Aiko",
        person_type="agent",
        message_channels=[
            {
                "name": channel.pop("name", "dev-chat"),
                "chat": {"notify_work_stops": True, **channel},
            }
            for channel in (dict(c) for c in channels)
        ],
    )


class _Context:
    """The member and its local chat."""

    def __init__(self, person: Person, *, chat: bool = True) -> None:
        self.person = person
        self.team = (
            chat_team(person)
            if chat
            else Team(project=Project(name="demo", services={}), members=[person])
        )
        self.chat = LocalChatService(person)

    def get_chat_service(self) -> LocalChatService:
        return self.chat


def _stop(reason: str = "asking", comment_url: str = COMMENT) -> WorkStop:
    return WorkStop(
        reason=reason,  # type: ignore[arg-type]
        run_id="run-1",
        url=TICKET,
        label="T-1",
        title="Fix login",
        comment_url=comment_url,
    )


@pytest.fixture
def events(monkeypatch) -> list[dict]:
    recorded: list[dict] = []
    monkeypatch.setattr(
        work_stops, "record_correlated_event", lambda **kwargs: recorded.append(kwargs)
    )
    return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_language", ["en", "ja"], indirect=True)
@pytest.mark.parametrize("reason", ["asking", "blocked", "failed"])
async def test_the_notice_is_posted_once_only_in_selected_channels(
    events, notice_language, reason
):
    context = _Context(
        _person(
            {"name": "dev", "channel_id": "C1"},
            {"name": "ops", "channel_id": "C2", "enabled": True},
            {"name": "quiet", "channel_id": "C3", "enabled": False},
            {"name": "unselected", "channel_id": "C4", "notify_work_stops": False},
            {"name": "duplicate", "channel_id": "C1"},
        )
    )

    await notify_work_stop(context, _stop(reason))  # type: ignore[arg-type]

    text = "\n".join(
        [
            t(
                f"capabilities.work_stops.{reason}",
                name="Aiko",
                item="T-1 Fix login",
                url=TICKET,
            ),
            t("capabilities.work_stops.comment", url=COMMENT),
        ]
    )
    status = WorkflowStatus(
        reason=reason,  # type: ignore[arg-type]
        person_id="aiko",
        run_id="run-1",
        subject=TICKET,
    )
    for channel_id in ("C1", "C2"):
        [line] = lines(channel_id)
        assert (line["author"], line["text"], line["status"]) == (
            username("aiko"),
            text,
            status.payload(),
        )
        # Every member reads it as a status, which nobody answers.
        [event] = (await context.chat.list_channel_events(channel_id)).events
        assert event.status == status
    assert not channel_path("C3").exists()
    assert not channel_path("C4").exists()
    assert [
        (event["event_type"], event["person_id"], event["payload"]) for event in events
    ] == [
        (
            "work_stop.notified",
            "aiko",
            {
                "run_id": "run-1",
                "reason": reason,
                "url": TICKET,
                "comment_url": COMMENT,
                "channel_id": channel_id,
                "channel_name": name,
                "delivered": True,
                "error": "",
            },
        )
        for channel_id, name in (("C1", "dev"), ("C2", "ops"))
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("events")
async def test_a_stop_without_a_comment_address_links_only_its_item():
    context = _Context(_person({"channel_id": "C1"}))

    await notify_work_stop(context, _stop(comment_url=""))  # type: ignore[arg-type]

    [line] = lines("C1")
    assert line["text"] == t(
        "capabilities.work_stops.asking", name="Aiko", item="T-1 Fix login", url=TICKET
    )


@pytest.mark.asyncio
async def test_a_channel_named_only_by_its_name_is_resolved(events):
    say("dev-chat", "hello", message_id="1")
    context = _Context(_person({"name": "dev-chat", "enabled": True}))

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    assert [line["text"] for line in lines("dev-chat")][0] == "hello"
    assert len(lines("dev-chat")) == 2
    assert events[0]["payload"]["channel_id"] == "dev-chat"
    assert events[0]["payload"]["delivered"] is True


@pytest.mark.asyncio
async def test_a_channel_that_cannot_be_resolved_is_recorded_undelivered(events):
    context = _Context(_person({"name": "nowhere", "enabled": True}))

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    assert not channel_path("nowhere").exists()
    [event] = events
    assert (
        event["payload"]["channel_id"],
        event["payload"]["channel_name"],
        event["payload"]["delivered"],
    ) == ("", "nowhere", False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channels", "chat"),
    [
        # The project has no chat.
        ([{"channel_id": "C1"}], False),
        # The member watches no channel.
        ([], True),
        ([{"channel_id": "C1", "enabled": False}], True),
        ([{"channel_id": "C1", "notify_work_stops": False}], True),
    ],
)
async def test_a_member_with_nowhere_to_say_it_posts_nothing(events, channels, chat):
    context = _Context(_person(*channels), chat=chat)

    def no_chat() -> None:
        raise AssertionError("no chat to post to")

    context.get_chat_service = no_chat  # type: ignore[method-assign]

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    assert events == []


@pytest.mark.asyncio
async def test_existing_channels_without_a_notification_setting_post_nothing(events):
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type="agent",
        message_channels=[{"name": "dev", "chat": {"channel_id": "C1"}}],
    )
    await notify_work_stop(_Context(person), _stop())  # type: ignore[arg-type]
    assert not channel_path("C1").exists()
    assert events == []


@pytest.mark.asyncio
async def test_a_selected_name_and_id_resolving_to_the_same_channel_post_once(events):
    say("C1", "hello", message_id="1")
    context = _Context(_person({"name": "C1"}, {"channel_id": "C1"}))
    await notify_work_stop(context, _stop())  # type: ignore[arg-type]
    assert len(lines("C1")) == 2
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_failed_post_is_recorded_and_not_raised(events, monkeypatch):
    context = _Context(_person({"channel_id": "C1"}, {"channel_id": "C2"}))
    post = context.chat.post_message

    async def failing(channel_id: str, *args, **kwargs):
        if channel_id == "C1":
            raise ServiceError("chat is down")
        return await post(channel_id, *args, **kwargs)

    monkeypatch.setattr(context.chat, "post_message", failing)

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    # The other channel still hears of it.
    assert len(lines("C2")) == 1
    assert [
        (
            event["payload"]["channel_id"],
            event["payload"]["delivered"],
            event["payload"]["error"],
        )
        for event in events
    ] == [("C1", False, "chat is down"), ("C2", True, "")]


@pytest.mark.asyncio
async def test_an_unexpected_failure_records_only_its_kind(events, monkeypatch):
    context = _Context(_person({"channel_id": "C1"}))

    async def failing(*_args, **_kwargs):
        raise RuntimeError("token xoxb-secret leaked in a message")

    monkeypatch.setattr(context.chat, "post_message", failing)

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    [event] = events
    assert (event["payload"]["delivered"], event["payload"]["error"]) == (
        False,
        "RuntimeError",
    )


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_written_does_not_raise(monkeypatch):
    context = _Context(_person({"channel_id": "C1"}))

    def failing(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(work_stops, "record_correlated_event", failing)

    await notify_work_stop(context, _stop())  # type: ignore[arg-type]

    assert len(lines("C1")) == 1


#: How a run's member can say it ended, and whether that leaves the work
#: waiting for a person.
_COMPLETIONS_WAITING = {
    "done": False,
    "asking": True,
    "blocked": True,
    # The checks resume it.
    "awaiting_checks": False,
}
#: Every status a run leaves, and whether its stop is announced: a failure the
#: run said on its item waits for a person; a rate limit and the checks resume
#: the work on their own, and a review limit is announced on the PR itself.
_STATUSES_ANNOUNCED = {
    "failed": True,
    "rate_limited": False,
    "review_limit": False,
    "awaiting_checks": False,
    "checks_settled": False,
    "asking": True,
    "blocked": True,
}


def test_every_completion_and_status_is_classified():
    """A new completion or status is decided on here, or this fails."""
    assert set(_COMPLETIONS_WAITING) == set(TASK_RUN_COMPLETIONS)
    assert set(_STATUSES_ANNOUNCED) == set(get_args(WorkflowStatusReason))
    assert {status for status, waits in _COMPLETIONS_WAITING.items() if waits} | {
        "failed"
    } == set(work_stops.STOP_REASONS)
    assert {
        status for status, announced in _STATUSES_ANNOUNCED.items() if announced
    } == set(work_stops.STOP_REASONS)


@pytest.mark.parametrize("status", TASK_RUN_COMPLETIONS)
def test_a_completion_is_a_stop_only_when_it_waits_for_a_person(status):
    store = RunStore()
    store.append_evidence(
        "run-1", "issue_comment", {"url": COMMENT, "target": {"url": TICKET}}
    )
    store.append_evidence("run-1", "checks_awaited", {"target": {"url": TICKET}})
    store.complete("run-1", status, "summary", Work.of_ticket(TICKET), "aiko")
    task = Task(id=TICKET, url=TICKET, title="Fix login", label="T-1", description="")

    assert work_stops.completed_stop(task, "run-1", chat_team(_person())) == (
        WorkStop(
            reason=status,  # type: ignore[arg-type]
            run_id="run-1",
            url=TICKET,
            label="T-1",
            title="Fix login",
            comment_url=COMMENT,
        )
        if _COMPLETIONS_WAITING[status]
        else None
    )


def test_a_run_that_recorded_no_completion_is_no_stop():
    task = Task(id=TICKET, url=TICKET, title="Fix login", description="")

    assert work_stops.completed_stop(task, "run-missing", chat_team(_person())) is None


def test_only_the_ticket_selector_announces_a_stop():
    """Every route of ticket work settles its run in the ticket selector, so
    a stop is announced there, once, and nowhere else."""
    package = Path(guildbotics.__file__).parent
    callers = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", ""))
        == "notify_work_stop"
    }

    assert callers == {"drivers/ticket_selector.py"}
