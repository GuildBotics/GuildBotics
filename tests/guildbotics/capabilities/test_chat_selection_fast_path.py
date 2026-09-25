"""Fast-path execution retains the workflow's receipt and completion contract."""

from types import SimpleNamespace

import pytest

from guildbotics.capabilities.task_runs import RunStore
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
from guildbotics.integrations.chat_state_store import ThreadContextUnavailableError
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.capabilities import chat_selection
from tests.guildbotics.capabilities.test_chat_selection import (
    FakeChatService,
    FakeInvokeContext,
    _dispatch,
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
@pytest.mark.parametrize("stored_effort", ["", "high"])
@pytest.mark.parametrize(
    "route,reaction,visible", [("no-op", "", False), ("reaction-only", "support", True)]
)
async def test_fast_path_records_evidence_before_completing(
    chat, monkeypatch, route, reaction, visible, stored_effort
):
    context, store, service = chat
    state = store.load_thread_state("slack", "alice", "C1", "100.1")
    state.effort = stored_effort
    store.save_thread_state("slack", "alice", "C1", "100.1", state)
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        return Selection(route=route, reaction=reaction, reason="5.test"), "f" * 32

    completed = []
    monkeypatch.setattr(chat_selection, "assess", assess)
    monkeypatch.setattr(
        chat_selection,
        "record_workflow_completed",
        lambda **kwargs: completed.append(kwargs),
    )
    await _dispatch(context, chat_service=service, state_store=store)
    assert not context.invocations
    assert len(seen) == 1
    assert seen[0]["reaction_target"] == "100.1"
    run_id = seen[0]["run_id"]
    assert RunStore().status(run_id).status == "done"
    # The run's completion is on record even though no workflow ran.
    assert completed == [{"run_id": run_id, "attempt": 1}]
    types = [e["evidence_type"] for e in RunStore().evidence(run_id)]
    assert "chat_decision" not in types
    assert types.index("chat_batch") < types.index(
        "chat_reaction" if visible else "chat_noop"
    )
    state = store.load_thread_state("slack", "alice", "C1", "100.1")
    assert ("alice" in state.participants) == visible
    assert state.effort == stored_effort
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
        return Selection(route="no-op", reason="none"), "f" * 32

    monkeypatch.setattr(chat_selection, "assess", assess)
    with pytest.raises(ThreadContextUnavailableError):
        await _dispatch(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert not service.reactions


@pytest.mark.asyncio
async def test_new_input_during_evaluation_is_reconsidered(chat, monkeypatch):
    context, store, service = chat

    async def assess(*args, **kwargs):
        return Selection(
            route="reaction-only",
            reaction="ack",
            reason="reaction",
        ), "f" * 32

    monkeypatch.setattr(chat_selection, "assess", assess)
    monkeypatch.setattr(
        chat_selection, "check_chat_updates", lambda *args: {"status": "new_messages"}
    )
    with pytest.raises(ThreadContextUnavailableError):
        await _dispatch(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert not service.reactions


@pytest.mark.asyncio
async def test_reaction_failure_never_completes(chat, monkeypatch):
    context, store, service = chat
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        return Selection(
            route="reaction-only", reaction="agree", reason="reaction"
        ), "f" * 32

    async def fail(*args):
        raise RuntimeError("reaction failed")

    monkeypatch.setattr(chat_selection, "assess", assess)
    monkeypatch.setattr(service, "add_reaction", fail)
    with pytest.raises(ThreadContextUnavailableError, match="remains pending"):
        await _dispatch(context, chat_service=service, state_store=store)
    assert not store.load_channel_cursor("slack", "alice", "C1").processed_event_ids
    assert all(
        e["evidence_type"] != "chat_reaction"
        for e in RunStore().evidence(seen[0]["run_id"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["evidence", "completion"])
@pytest.mark.parametrize("edited", [False, True])
async def test_reaction_recovers_without_duplicate_visible_action(
    chat, monkeypatch, failure, edited
):
    context, state_store, service = chat
    seen = []

    async def assess(state, *args, **kwargs):
        seen.append(state)
        if len(seen) > 1 and edited:
            return Selection(route="agent", reason="request"), "e" * 32
        return Selection(
            route="reaction-only", reaction="ack", reason="reaction"
        ), "f" * 32

    async def idempotent_reaction(channel, ts, reaction):
        item = (channel, ts, reaction)
        if item not in service.reactions:
            service.reactions.append(item)

    monkeypatch.setattr(chat_selection, "assess", assess)
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
        await _dispatch(context, chat_service=service, state_store=state_store)
    assert not state_store.load_channel_cursor(
        "slack", "alice", "C1"
    ).processed_event_ids
    run_id = seen[0]["run_id"]
    if edited:
        _set_incoming_event(context, text="@alice please investigate another issue")
    context.retry_context = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": run_id,
    }
    await _dispatch(context, chat_service=service, state_store=state_store)
    assert service.reactions == [("C1", "100.1", "ack")]
    # A re-dispatch judges again rather than reusing the first judgment.
    assert len(seen) == 2
    assert bool(context.invocations) == edited
    assert RunStore().status(run_id).status == "done"
    assert len(
        [
            e
            for e in RunStore().evidence(run_id)
            if e["evidence_type"] == "chat_reaction"
        ]
    ) == (0 if edited and failure == "evidence" else 1)


@pytest.mark.asyncio
async def test_failed_judgment_agent_fallback_escalates_on_final_attempt(
    chat, monkeypatch
):
    context, store, service = chat
    context.action = "crash"
    context.retry_context = {
        "attempt_count": 2,
        "max_attempts": 2,
        "is_final_attempt": True,
        "run_id": "last-attempt",
    }

    async def assess(*args, **kwargs):
        return Selection(
            route="agent",
            reason="invalid",
        ), "f" * 32

    monkeypatch.setattr(chat_selection, "assess", assess)
    await _dispatch(context, chat_service=service, state_store=store)
    assert store.load_channel_cursor("slack", "alice", "C1").processed_event_ids == [
        "E1"
    ]
    assert len(service.posts) == 1
