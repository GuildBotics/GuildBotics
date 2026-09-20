"""Fast-path execution retains the workflow's receipt and completion contract."""

from types import SimpleNamespace

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
from guildbotics.integrations.chat_state_store import ThreadContextUnavailableError
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.runtime.workflow_invocation import WORKFLOW_INVOCATION_KEY
from guildbotics.templates.commands.workflows import (
    chat_conversation_workflow as workflow,
)
from tests.guildbotics.templates.commands.workflows.test_chat_conversation_workflow import (
    FakeChatService,
    FakeInvokeContext,
    _set_incoming_event,
)


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    context = FakeInvokeContext("reply")
    context.team = SimpleNamespace(members=[])
    _set_incoming_event(context)
    store = FileConversationStateStore()
    service = FakeChatService()
    ChatReceiveStatus().save("slack", "alice", "C1", state="ready")
    return context, store, service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,reaction,visible", [("no-op", "", False), ("reaction-only", "support", True)]
)
async def test_fast_path_records_evidence_before_completing(
    chat, monkeypatch, route, reaction, visible
):
    context, store, service = chat
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        return Selection(route=route, reaction=reaction, reason="5.test"), "f" * 32

    monkeypatch.setattr(workflow, "assess", assess)
    await workflow.main(context, chat_service=service, state_store=store)
    assert not context.invocations
    assert len(seen) == 1
    assert seen[0]["reaction_target"] == "100.1"
    run_id = seen[0]["run_id"]
    assert RunStore().status(run_id).status == "done"
    types = [e["evidence_type"] for e in RunStore().evidence(run_id)]
    assert types.index("chat_batch") < types.index("chat_decision")
    assert ("chat_reaction" if visible else "chat_noop") in types
    state = store.load_thread_state("slack", "alice", "C1", "100.1")
    assert ("alice" in state.participants) == visible
    assert state.effort == ""
    assert "E1" in store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert service.reactions == ([("C1", "100.1", reaction)] if visible else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("receive", ["catching_up", "unavailable"])
async def test_unavailable_receiver_never_confirms_a_fast_path(
    chat, monkeypatch, receive
):
    context, store, service = chat

    async def assess(*args, **kwargs):
        ChatReceiveStatus().save("slack", "alice", "C1", state=receive)
        return Selection(route="no-op", reason="5.none"), "f" * 32

    monkeypatch.setattr(workflow, "assess", assess)
    with pytest.raises(ThreadContextUnavailableError):
        await workflow.main(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert not service.reactions


@pytest.mark.asyncio
async def test_new_input_during_evaluation_is_reconsidered(chat, monkeypatch):
    context, store, service = chat

    async def assess(*args, **kwargs):
        return Selection(
            route="reaction-only", reaction="ack", reason="5.reaction"
        ), "f" * 32

    monkeypatch.setattr(workflow, "assess", assess)
    monkeypatch.setattr(
        workflow, "check_chat_updates", lambda *args: {"status": "new_messages"}
    )
    with pytest.raises(ThreadContextUnavailableError):
        await workflow.main(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert not service.reactions


@pytest.mark.asyncio
async def test_reaction_failure_never_completes(chat, monkeypatch):
    context, store, service = chat
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        return Selection(
            route="reaction-only", reaction="agree", reason="5.reaction"
        ), "f" * 32

    async def fail(*args):
        raise RuntimeError("reaction failed")

    monkeypatch.setattr(workflow, "assess", assess)
    monkeypatch.setattr(service, "add_reaction", fail)
    with pytest.raises(ThreadContextUnavailableError, match="remains pending"):
        await workflow.main(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert all(
        e["evidence_type"] != "chat_reaction"
        for e in RunStore().evidence(seen[0]["run_id"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["evidence", "completion"])
async def test_reaction_recovers_without_duplicate_visible_action(
    chat, monkeypatch, failure
):
    context, state_store, service = chat
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        return Selection(
            route="reaction-only", reaction="ack", reason="5.reaction"
        ), "f" * 32

    async def idempotent_reaction(channel, ts, reaction):
        item = (channel, ts, reaction)
        if item not in service.reactions:
            service.reactions.append(item)

    monkeypatch.setattr(workflow, "assess", assess)
    monkeypatch.setattr(service, "add_reaction", idempotent_reaction)
    method = "append_evidence" if failure == "evidence" else "complete_run"
    original = getattr(RunStore, method)
    failed = False

    def fail_once(self, *args, **kwargs):
        nonlocal failed
        if not failed and (failure == "completion" or args[1] == "chat_reaction"):
            failed = True
            raise OSError("disk full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(RunStore, method, fail_once)
    with pytest.raises(ThreadContextUnavailableError):
        await workflow.main(context, chat_service=service, state_store=state_store)
    assert not state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids
    run_id = seen[0]["run_id"]
    context.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": run_id,
    }
    await workflow.main(context, chat_service=service, state_store=state_store)
    assert service.reactions == [("C1", "100.1", "ack")]
    assert RunStore().status(run_id).status == "done"
    assert (
        len(
            [
                e
                for e in RunStore().evidence(run_id)
                if e["evidence_type"] == "chat_reaction"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_failed_judgment_and_failed_agent_remain_pending_on_final_attempt(
    chat, monkeypatch
):
    context, store, service = chat
    context.action = "crash"
    context.shared_state[WORKFLOW_INVOCATION_KEY].payload["retry_context"] = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": "last-attempt",
    }

    async def assess(*args, **kwargs):
        return Selection(
            route="agent",
            effort="high",
            reason="1.invalid",
            effort_reason="effort.failure",
        ), "f" * 32

    monkeypatch.setattr(workflow, "assess", assess)
    with pytest.raises(ThreadContextUnavailableError):
        await workflow.main(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
