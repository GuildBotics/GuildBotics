"""The VM judgment uses the host's freshness and completion gates."""

import json
import logging
from types import SimpleNamespace

import pytest

from guildbotics.guest.decisions import JudgmentIncomplete, judge_chat
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.runtime.member_invocation import ChatSubject
from guildbotics.runtime.workflow_invocation import ChatTurn


class Client:
    def __init__(self, *, changed=False, stale=False, failed=""):
        self.calls = []
        self.changed = changed
        self.stale = stale
        self.failed = failed

    def call(self, name, **_):
        self.calls.append(name)
        if name == self.failed:
            raise OSError("unavailable completion")
        return [] if name == "evidence" else None

    async def acall(self, name, *, arguments, stdin):
        assert name == "member"
        assert arguments[-2:] == ["--person", "aiko"]
        command = " ".join(arguments[:2])
        self.calls.append((arguments, stdin))
        result = {}
        if command == "chat updates":
            result = {"status": "unavailable" if self.stale else "up_to_date"}
        elif command == "chat inspect":
            result = {
                "next_cursor": "",
                "messages": [{"text": "edited" if self.changed else "original"}],
            }
        elif command == "chat complete":
            assert "--status" in arguments and "done" in arguments
        return {"exit_code": 0, "stdout": json.dumps(result), "stderr": ""}


def turn():
    return ChatTurn(
        attempt=1,
        subject=ChatSubject("local", "C1", "T1", "E1", "local-aiko"),
        message_id="M1",
        context_cursor="cursor",
        effort="default",
        prompt={
            "source_snapshot": [{"text": "original"}],
            "thread_context_complete": True,
        },
    )


def fixture(monkeypatch, selection, **options):
    client = Client(**options)
    monkeypatch.setattr("guildbotics.guest.host_client.command_window", lambda: client)
    context = SimpleNamespace(
        person=SimpleNamespace(person_id="aiko", roles={"architect": {}}, profile={}),
        logger=logging.getLogger(__name__),
        brain_factory=object(),
    )
    states = []

    async def assess(state, config, **kwargs):
        assert config is None
        assert kwargs["brain_factory"] is context.brain_factory
        assert kwargs["recorder"].client is client
        states.append(state)
        return selection, "evaluation"

    monkeypatch.setattr("guildbotics.intelligences.decisions.assessment.assess", assess)
    return context, client, states


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "arrival", "repeated_cursor"])
async def test_fast_path_rechecks_every_page_and_the_same_bounded_history(
    monkeypatch, change
):
    context, client, _ = fixture(
        monkeypatch, Selection(route="no-op", reason="ambient")
    )
    messages = [
        {
            "message_id": f"M{i:03}",
            "occurred_at": "2026-11-05T00:00:00Z",
            "text": str(i),
        }
        for i in range(1, 151)
    ]
    selected = turn()
    selected.prompt["source_cursor"] = "2026-11-05T00:00:00.000000Z M150"
    selected.prompt["source_snapshot"] = messages[49:]
    original = client.acall
    pages = []

    async def call(name, *, arguments, stdin):
        if arguments[:3] != ["chat", "inspect", "thread"]:
            return await original(name, arguments=arguments, stdin=stdin)
        pages.append(arguments)
        second = "--cursor" in arguments
        rows = messages[100:] if second else messages[:100]
        if second and change == "arrival":
            rows = [
                *rows,
                {
                    "message_id": "M151",
                    "occurred_at": "2026-11-05T00:00:00Z",
                    "text": "new",
                },
            ]
        cursor = "page2" if not second or change == "repeated_cursor" else ""
        return {
            "exit_code": 0,
            "stdout": json.dumps({"messages": rows, "next_cursor": cursor}),
            "stderr": "",
        }

    monkeypatch.setattr(client, "acall", call)
    if change == "none":
        assert await judge_chat(context, selected) is False
        assert "require_completion" in client.calls
    else:
        with pytest.raises(JudgmentIncomplete):
            await judge_chat(context, selected)
        assert "require_completion" not in client.calls
    assert len(pages) == 2
    assert "--cursor" not in pages[0]
    assert pages[1][pages[1].index("--cursor") + 1] == "page2"


@pytest.mark.asyncio
async def test_agent_judgment_promotes_effort_and_preserves_batch_context(monkeypatch):
    context, client, states = fixture(
        monkeypatch, Selection(route="agent", reason="work", response_effort="high")
    )
    selected = turn()
    assert await judge_chat(context, selected) is True
    assert selected.effort == "high"
    assert client.calls == ["evidence"]
    assert states[0]["member"]["roles"] == ["architect"]
    assert states[0]["event_ids"] == ["E1"]
    assert states[0]["previous_outcomes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route,reaction", [("no-op", None), ("reaction-only", "ack")])
async def test_fast_paths_recheck_provider_and_queue_before_member_completion(
    monkeypatch, route, reaction
):
    context, client, _ = fixture(
        monkeypatch, Selection(route=route, reason="handled", reaction=reaction)
    )
    assert await judge_chat(context, turn()) is False
    commands = [call[0] for call in client.calls if isinstance(call, tuple)]
    assert [args[1] for args in commands] == [
        "updates",
        "inspect",
        "updates",
        "noop" if route == "no-op" else "reaction",
        "updates",
        "complete",
    ]
    assert client.calls[-2:] == ["require_completion", "record_completed"]
    if reaction:
        assert commands[3][commands[3].index("--message-id") + 1] == "M1"
        assert commands[3][commands[3].index("--reaction") + 1] == reaction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options", [{"changed": True}, {"stale": True}, {"failed": "require_completion"}]
)
async def test_unconfirmed_fast_path_remains_pending_and_never_starts_an_agent(
    monkeypatch, options
):
    context, client, _ = fixture(
        monkeypatch, Selection(route="no-op", reason="handled"), **options
    )
    with pytest.raises(JudgmentIncomplete):
        await judge_chat(context, turn())
    if "failed" not in options:
        assert not any(
            isinstance(call, tuple) and call[0][1] in ("noop", "reaction", "complete")
            for call in client.calls
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["receipt", "completion", "reaction"])
@pytest.mark.parametrize("edited", [False, True])
async def test_a_failed_fast_path_rejudges_and_keeps_visible_reactions_idempotent(
    monkeypatch, failure, edited
):
    context, client, states = fixture(
        monkeypatch, Selection(route="reaction-only", reaction="ack", reason="handled")
    )
    original = client.acall
    visible = set()
    receipts = []
    failed = False

    async def call(name, *, arguments, stdin):
        nonlocal failed
        command = " ".join(arguments[:2])
        if command == "chat reaction":
            if failure == "reaction" and not failed:
                failed = True
                return {"exit_code": 1, "stdout": "", "stderr": "reaction failed"}
            visible.add(("C1", "M1", "ack"))
            if failure == "receipt" and not failed:
                failed = True
                return {"exit_code": 1, "stdout": "", "stderr": "receipt failed"}
            if "reaction" not in receipts:
                receipts.append("reaction")
        if command == "chat complete" and failure == "completion" and not failed:
            failed = True
            return {"exit_code": 1, "stdout": "", "stderr": "completion failed"}
        return await original(name, arguments=arguments, stdin=stdin)

    monkeypatch.setattr(client, "acall", call)
    with pytest.raises(JudgmentIncomplete):
        await judge_chat(context, turn())
    assert "require_completion" not in client.calls
    if edited:

        async def reassess(state, *_args, **_kwargs):
            states.append(state)
            return Selection(route="agent", reason="request"), "evaluation-2"

        monkeypatch.setattr(
            "guildbotics.intelligences.decisions.assessment.assess", reassess
        )
    assert await judge_chat(context, turn()) is edited
    assert len(states) == 2
    assert len(visible) == (0 if failure == "reaction" and edited else 1)
    assert receipts == ([] if edited and failure != "completion" else ["reaction"])
    assert ("require_completion" in client.calls) is not edited
