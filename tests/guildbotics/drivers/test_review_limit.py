"""Review-limit decisions are settled by the selector, outside agent turns."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.integrations.local import store
from guildbotics.integrations.local.chat import channel_path
from guildbotics.integrations.pull_request_patrol import REVIEW_LIMIT
from guildbotics.observability.activity_event_store import ActivityEventStore
from guildbotics.observability.trace_status import resolve_trace_status
from guildbotics.runtime.port import ServiceError, ServiceUnavailable
from guildbotics.utils.correlation import current_trace, trace_scope
from tests.guildbotics.drivers.test_ticket_selector import CHANNEL, _Context, _task
from tests.guildbotics.local_chat import lines
from tests.guildbotics.local_code_host import item, pull_request, remote_with, write


@pytest.fixture
def events(monkeypatch):
    from guildbotics.capabilities import review_limits
    from guildbotics.utils.correlation import correlation_fields

    captured = []
    record = review_limits.record_correlated_event

    def capture(**kwargs):
        captured.append(
            {
                **correlation_fields(),
                **kwargs,
                "kind": "event",
                "type": kwargs["event_type"],
            }
        )
        record(**kwargs)

    monkeypatch.setattr(review_limits, "record_correlated_event", capture)
    return captured


def _limited_pull():
    remote_with("feature")
    url = pull_request(1, "feature")
    pull = item(url)
    pull["reviews"] = [
        {
            "id": f"review-{n}",
            "author": "aiko",
            "body": "",
            "verdict": "approve",
            "commit": f"head-{n}",
            "created_at": f"2026-01-0{n}T00:00:00Z",
        }
        for n in range(1, 4)
    ]
    write(store.locate(url, "pull_request"), pull)
    return url


@pytest.mark.asyncio
async def test_provider_only_returns_the_review_limit_decision():
    context = _Context()
    url = _limited_pull()
    [task] = await context.member.code.pull_request_candidates()
    assert task.trigger_reason == REVIEW_LIMIT
    assert not item(url)["draft"]
    assert item(url)["comments"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["manual", "scheduled", "routine"])
async def test_listing_hands_over_in_its_own_trace_and_runs_the_next_ticket(
    monkeypatch, source, events
):
    context = _Context()
    url = _limited_pull()
    task = _task()
    turns = []
    comment = context.member.code.comment

    async def with_url(*args, **kwargs):
        written = await comment(*args, **kwargs)
        return written.model_copy(update={"url": f"{url}#{written.id}"})

    monkeypatch.setattr(context.member.code, "comment", with_url)

    async def run(invocation):
        turns.append(invocation)
        return "done"

    with trace_scope(source, trace_id="outer", person_id="aiko"):
        assert (
            await TicketSelector(context, source=source).run_next(context.person, run)
            == "done"
        )
        assert current_trace().trace_id == "outer"

    assert item(url)["draft"]
    [notice] = item(url)["comments"]
    assert notice["status"]["reason"] == "review_limit"
    assert notice["status"]["run_id"] != "outer"
    [message] = lines(CHANNEL)
    assert message["status"]["reason"] == "review_limit"
    assert url in message["text"] and notice["id"] in message["text"]
    assert len(turns) == 1
    assert turns[0].payload["ticket_url"] == task.url
    assert [e["type"] for e in events] == [
        "review_limit.started",
        "pull_request.review_limit",
        "review_limit.finished",
    ]
    assert {e["trace_id"] for e in events} == {notice["status"]["run_id"]}
    assert resolve_trace_status(events) == "success"
    assert list(RunStore().records()) == []
    now = datetime.now(UTC)
    [fact] = ActivityEventStore().records_between(
        now - timedelta(days=1), now + timedelta(days=1)
    )
    assert fact["type"] == "pull_request.review_limit"
    assert fact["payload"]["comment_url"] == f"{url}#{notice['id']}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [ServiceError("refused"), ServiceUnavailable("offline")]
)
async def test_conversion_failure_is_reported_but_unavailability_is_deferred(
    monkeypatch, failure, events
):
    context = _Context()
    url = _limited_pull()

    async def fail(url):
        raise failure

    monkeypatch.setattr(context.member.code, "convert_pull_request_to_draft", fail)
    assert (
        await TicketSelector(context, source="manual").candidates(context.person) == []
    )
    assert not item(url)["draft"]
    if isinstance(failure, ServiceUnavailable):
        assert item(url)["comments"] == []
        assert not channel_path(CHANNEL).exists()
        assert events[-1]["type"] == "review_limit.deferred"
        assert resolve_trace_status(events) == "info"
    else:
        assert item(url)["comments"][0]["status"]["reason"] == "failed"
        assert lines(CHANNEL)[0]["status"]["reason"] == "failed"
        assert events[-1]["type"] == "review_limit.failed"
        assert resolve_trace_status(events) == "failed"
    assert all(e["type"] != "pull_request.review_limit" for e in events)


@pytest.mark.asyncio
async def test_refresh_hands_over_and_duplicate_decisions_do_not_notify_twice(events):
    context = _Context()
    url = _limited_pull()
    [task] = await context.member.code.pull_request_candidates()
    selector = TicketSelector(context)
    assert await asyncio.gather(
        selector.refresh(context.person, task), selector.refresh(context.person, task)
    ) == [None, None]
    assert len(item(url)["comments"]) == 1
    assert len(lines(CHANNEL)) == 1
    assert sum(e["type"] == "pull_request.review_limit" for e in events) == 1


@pytest.mark.asyncio
async def test_a_failed_pr_notice_still_records_and_notifies(monkeypatch, events):
    context = _Context()
    url = _limited_pull()

    async def fail(*args, **kwargs):
        raise ServiceError("comment refused")

    monkeypatch.setattr(context.member.code, "comment", fail)
    assert await TicketSelector(context).candidates(context.person) == []
    assert item(url)["draft"] and item(url)["comments"] == []
    [fact] = [e for e in events if e["type"] == "pull_request.review_limit"]
    assert fact["payload"]["comment_url"] == ""
    assert fact["payload"]["comment_error"] == "ServiceError"
    assert url in lines(CHANNEL)[0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["convert", "comment"])
async def test_cancellation_closes_its_trace_and_restores_the_outer_trace(
    monkeypatch, events, phase
):
    context = _Context()
    _limited_pull()

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    operation = "convert_pull_request_to_draft" if phase == "convert" else "comment"
    monkeypatch.setattr(context.member.code, operation, cancel)
    with trace_scope("manual", trace_id="outer"):
        with pytest.raises(asyncio.CancelledError):
            await TicketSelector(context).candidates(context.person)
        assert current_trace().trace_id == "outer"
    assert events[-1]["type"] == "review_limit.failed"
    assert events[-1]["payload"]["error"] == "CancelledError"
    assert sum(e["type"] == "pull_request.review_limit" for e in events) == (
        phase == "comment"
    )


@pytest.mark.asyncio
async def test_run_next_continues_when_the_first_candidate_reaches_the_limit_at_refresh(
    monkeypatch,
):
    context = _Context()
    url = _limited_pull()
    next_task = _task()
    [decision] = await context.member.code.pull_request_candidates()
    candidate = decision.model_copy(update={"trigger_reason": "pull_request_review"})
    selector = TicketSelector(context)

    async def candidates(person):
        return [candidate, next_task]

    async def run(invocation):
        return invocation.payload["ticket_url"]

    monkeypatch.setattr(selector, "candidates", candidates)
    with trace_scope("manual"):
        assert await selector.run_next(context.person, run) == next_task.url
    assert item(url)["draft"]


@pytest.mark.asyncio
async def test_a_human_who_drafted_after_selection_is_not_announced_as_the_host(
    monkeypatch, events
):
    context = _Context()
    url = _limited_pull()
    [decision] = await context.member.code.pull_request_candidates()
    pull = item(url)
    pull["draft"] = True
    write(store.locate(url, "pull_request"), pull)
    selector = TicketSelector(context)
    await selector._hand_over_at_review_limit(context, decision)
    assert item(url)["comments"] == []
    assert not channel_path(CHANNEL).exists()
    assert [e["type"] for e in events] == [
        "review_limit.started",
        "review_limit.finished",
    ]


@pytest.mark.asyncio
async def test_listing_settles_every_limited_pr_in_a_separate_trace(events):
    context = _Context()
    first = _limited_pull()
    second = pull_request(2, "feature", reviews=item(first)["reviews"])
    task = _task()
    [remaining] = await TicketSelector(context).candidates(context.person)
    assert remaining.url == task.url
    assert item(first)["draft"] and item(second)["draft"]
    facts = [e for e in events if e["type"] == "pull_request.review_limit"]
    assert len({e["trace_id"] for e in facts}) == 2
    assert {e["payload"]["pull_request"]["url"] for e in facts} == {first, second}
    assert len(lines(CHANNEL)) == 2


@pytest.mark.asyncio
async def test_two_reviewers_with_preselected_decisions_announce_only_the_winner(
    events,
):
    from tests.guildbotics.local_code_host import local_member
    from guildbotics.entities.team import Person

    context = _Context()
    url = _limited_pull()
    [decision] = await context.member.code.pull_request_candidates()
    other = _Context()
    other.person = Person(
        person_id="yuki",
        name="Yuki",
        person_type="agent",
        message_channels=context.person.message_channels,
    )
    other.member = local_member(other.person)
    other.team = other.member.team
    other.team.project.services["chat_service"] = {"name": "local"}
    selector = TicketSelector(context)
    await asyncio.gather(
        selector._hand_over_at_review_limit(context, decision),
        selector._hand_over_at_review_limit(other, decision),
    )
    assert item(url)["draft"]
    assert len(item(url)["comments"]) == 1
    assert len(lines(CHANNEL)) == 1
    assert sum(e["type"] == "pull_request.review_limit" for e in events) == 1


@pytest.mark.asyncio
async def test_a_deferred_conversion_is_tried_again_on_the_next_listing(
    monkeypatch, events
):
    context = _Context()
    url = _limited_pull()
    convert = context.member.code.convert_pull_request_to_draft

    async def unavailable(url):
        raise ServiceUnavailable("offline")

    monkeypatch.setattr(
        context.member.code, "convert_pull_request_to_draft", unavailable
    )
    selector = TicketSelector(context)
    assert await selector.candidates(context.person) == []
    assert not item(url)["draft"]
    monkeypatch.setattr(context.member.code, "convert_pull_request_to_draft", convert)
    assert await selector.candidates(context.person) == []
    assert item(url)["draft"]
    assert sum(e["type"] == "pull_request.review_limit" for e in events) == 1
