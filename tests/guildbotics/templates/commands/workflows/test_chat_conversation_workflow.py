"""The VM judges once and keeps its selected input across completion retries."""

from types import SimpleNamespace

import pytest

from guildbotics.commands.agent_turn import run_agent_turn
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.runtime.workflow_invocation import (
    WORKFLOW_INVOCATION_KEY,
    WorkflowInvocation,
)
from guildbotics.templates.commands.workflows import (
    chat_conversation_workflow as workflow,
)
from tests.guildbotics.commands.test_agent_turn import Ledger
from tests.guildbotics.guest.test_chat_judgment import fixture, turn


@pytest.mark.asyncio
@pytest.mark.parametrize("completes_on", [1, 2])
@pytest.mark.parametrize(
    "stored,candidate,expected,reason",
    [
        ("", "default", "default", "work"),
        ("", "high", "high", "work"),
        ("default", "high", "high", "work"),
        ("high", "default", "high", "work"),
        ("high", None, "high", "work"),
        ("default", None, "default", "work"),
        ("", None, "", "work"),
        ("", None, "", "invalid"),
        ("", None, "", "context"),
        ("", None, "", "request"),
    ],
)
async def test_one_judgment_feeds_explicit_response_input_and_effort_to_every_completion_attempt(
    tmp_path, monkeypatch, completes_on, stored, candidate, expected, reason
):
    context, client, assessments = fixture(
        monkeypatch, Selection(route="agent", reason=reason, response_effort=candidate)
    )
    selected = turn()
    selected.effort = stored
    selected.prompt.update(
        unprocessed_messages=[{"content": "please check", "author": "owner"}],
        participant_labels={"local-aiko": "aiko", "owner": "owner"},
        previous_thread_context={
            "thread_topic": "topic",
            "latest_focus": "focus",
            "handoffs": [],
        },
        handoff_candidates=[],
        chat_participation="strict",
        thread_context=[],
    )
    context.shared_state = {
        WORKFLOW_INVOCATION_KEY: WorkflowInvocation(
            command="workflows/chat_conversation_workflow",
            person_id="aiko",
            source="event_queue",
            trigger_type="chat",
            payload=selected.model_dump(),
        )
    }
    context.language_name = "Japanese"
    context.pipe = "another command's output"
    monkeypatch.setattr(workflow, "get_member_clone_path", lambda _: tmp_path)
    ledger = Ledger()
    responses = []
    invocations = []

    async def invoke(name, **arguments):
        invocations.append((name, arguments))
        assert arguments["message"] == ""
        assert arguments.get("effort", "") == expected
        assert "model" not in arguments and "brain" not in arguments
        assert arguments["agent_execution_context"]["retry_invoke_exceptions"] is False

        async def respond(execution, parameters):
            responses.append((execution, parameters))
            ledger.complete = len(responses) == completes_on
            return SimpleNamespace(status="done", message="completed")

        return await run_agent_turn(
            invoke=respond,
            execution_context=arguments["agent_execution_context"],
            ledger=ledger,
        )

    context.invoke = invoke
    await workflow.main(context)
    assert len(assessments) == len(invocations) == 1
    assert len(responses) == completes_on
    assert "previous_effort" not in assessments[0]
    assert invocations[0][0] == "functions/handle_chat_event"
    assert invocations[0][1]["cwd"] == tmp_path
    assert invocations[0][1]["member_workspace"] == str(tmp_path)
    assert invocations[0][1]["event_id"] == selected.subject.event_id
    assert invocations[0][1]["thread_id"] == selected.subject.thread_id
    assert ledger.completed == [completes_on]
    assert ledger.missing == ([1] if completes_on == 2 else [])
    assert client.calls == ["evidence"]
