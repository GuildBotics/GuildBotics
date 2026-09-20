"""New input must reach the agent before its source-thread work is published."""

import json
from dataclasses import replace

import pytest

from guildbotics.capabilities.chat_batch import chat_batch_event_ids
from guildbotics.capabilities.chat_updates import (
    ChatUpdatesRequired,
    check_chat_updates,
    ensure_chat_current,
)
from guildbotics.capabilities.task_runs import RUN_ENV, RunStore
from guildbotics.integrations import chat_receive_status
from guildbotics.integrations.chat_receive_status import ChatReceiveStatus
from guildbotics.integrations.chat_service import ChatEvent
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from tests.guildbotics.capabilities.test_member_chat import _service as chat_service
from tests.guildbotics.capabilities.test_member_github import (
    FakeClient,
    _service as github_service,
)


@pytest.fixture
def chat_run(monkeypatch):
    monkeypatch.setenv(RUN_ENV, "chat-run")
    RunStore().append_evidence(
        "chat-run",
        "chat_batch",
        {
            "person_id": "aiko",
            "service": "slack",
            "channel_id": "C1",
            "thread_ts": "100.1",
            "self_user_id": "U_BOT",
            "event_ids": ["E1"],
        },
    )
    ChatReceiveStatus().save("slack", "aiko", "C1", state="ready")
    return FileConversationStateStore()


def event(number, **kwargs):
    return ChatEvent(
        event_id=f"E{number}",
        channel_id="C1",
        thread_ts="100.1",
        message_ts=f"{100 + number}.1",
        author_id="U_OTHER",
        text=f"request {number}",
        **kwargs,
    )


def queue(store, message):
    store.upsert_pending_event("slack", "aiko", "C1", message)


def test_check_delivers_all_new_input_without_consuming_queue(chat_run):
    queue(chat_run, event(5))
    queue(chat_run, event(3))
    with pytest.raises(ChatUpdatesRequired, match="chat updates"):
        ensure_chat_current("aiko")
    result = check_chat_updates("aiko", "chat-run")
    assert result["status"] == "new_messages"
    assert [item["event_id"] for item in result["messages"]] == ["E3", "E5"]
    assert len(chat_run.load_pending_events("slack", "aiko", "C1")) == 2
    assert not chat_run.is_processed_event("slack", "aiko", "C1", "E3")
    assert chat_batch_event_ids(RunStore().evidence("chat-run")) == ["E1", "E3", "E5"]
    assert check_chat_updates("aiko", "chat-run")["status"] == "up_to_date"
    ensure_chat_current("aiko")
    queue(chat_run, event(6))
    with pytest.raises(ChatUpdatesRequired, match="chat updates"):
        ensure_chat_current("aiko")


def test_only_source_thread_external_unprocessed_input_is_new(chat_run):
    for message in (
        event(1),
        replace(event(2), author_id="U_BOT"),
        replace(event(3), thread_ts="other"),
        event(4, is_edit_or_delete=True),
        event(5),
    ):
        queue(chat_run, message)
    chat_run.mark_processed_event("slack", "aiko", "C1", "E5")
    assert check_chat_updates("aiko", "chat-run")["messages"] == []


@pytest.mark.parametrize("state", ["missing", "disconnected", "stale", "malformed"])
def test_unavailable_is_never_no_new_messages(chat_run, monkeypatch, state):
    status = ChatReceiveStatus()
    path = status._path("slack", "aiko", "C1")
    if state == "missing":
        path.unlink()
    elif state == "disconnected":
        status.save("slack", "aiko", "C1", state="unavailable")
    elif state == "malformed":
        path.write_text("[]")
    else:
        now = json.loads(path.read_text())["checked_at"]
        monkeypatch.setattr(chat_receive_status.time, "time", lambda: now + 16)
    assert check_chat_updates("aiko", "chat-run")["status"] == "unavailable"
    assert len(RunStore().evidence("chat-run")) == 1
    with pytest.raises(ChatUpdatesRequired):
        ensure_chat_current("aiko")


def test_member_and_active_run_must_match(chat_run):
    with pytest.raises(ChatUpdatesRequired):
        check_chat_updates("other", "chat-run")
    with pytest.raises(ChatUpdatesRequired):
        check_chat_updates("aiko", "other-run")


def test_new_batch_resets_checked_membership(chat_run):
    check_chat_updates("aiko", "chat-run")
    source = RunStore().evidence("chat-run")[0]["payload"]
    RunStore().append_evidence(
        "chat-run", "chat_batch", {**source, "event_ids": ["E7"]}
    )
    assert chat_batch_event_ids(RunStore().evidence("chat-run")) == ["E7"]
    with pytest.raises(ChatUpdatesRequired):
        ensure_chat_current("aiko")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reply", "post", "reaction"])
async def test_chat_write_checks_source_even_for_another_destination(chat_run, action):
    service = chat_service()

    async def publish():
        kwargs = {"channel_id": "C_OTHER", "channel_name": None}
        if action == "reaction":
            return await service.add_reaction(
                **kwargs, message_ts="200.1", reaction="ack"
            )
        if action == "reply":
            kwargs["thread_ts"] = "200.1"
        return await getattr(service, action)(**kwargs, body="response")

    with pytest.raises(ChatUpdatesRequired):
        await publish()
    assert not service.chat_service.posts and not service.chat_service.reactions
    check_chat_updates("aiko", "chat-run")
    queue(chat_run, event(3))
    with pytest.raises(ChatUpdatesRequired):
        await publish()
    check_chat_updates("aiko", "chat-run")
    await publish()
    assert len(service.chat_service.posts) + len(service.chat_service.reactions) == 1


@pytest.mark.asyncio
async def test_github_comment_checks_original_chat(chat_run):
    service = github_service()
    client = FakeClient()
    client.get_payloads["/repos/owner/repo/issues/1"] = {"number": 1, "title": "T"}
    service._client = client
    with pytest.raises(ChatUpdatesRequired):
        await service.issue_comment("https://github.com/owner/repo/issues/1", "comment")
    # A refused write reaches GitHub for nothing, not even to read the issue.
    assert not client.posts and not client.gets
    check_chat_updates("aiko", "chat-run")
    await service.issue_comment("https://github.com/owner/repo/issues/1", "comment")
    assert len(client.posts) == 1


def test_interactive_and_ticket_runs_have_no_chat_precondition(monkeypatch):
    monkeypatch.delenv(RUN_ENV, raising=False)
    ensure_chat_current("aiko")
    monkeypatch.setenv("GUILDBOTICS_TASK_RUN_ID", "ticket-run")
    ensure_chat_current("aiko")


def test_pending_reads_the_processed_cursor_once(chat_run, monkeypatch):
    for number in range(2, 22):
        queue(chat_run, event(number))
    reads = []
    original = FileConversationStateStore.load_channel_cursor

    def load(store, *scope):
        reads.append(scope)
        return original(store, *scope)

    monkeypatch.setattr(FileConversationStateStore, "load_channel_cursor", load)
    result = check_chat_updates("aiko", "chat-run")
    assert len(result["messages"]) == 20
    assert reads == [("slack", "aiko", "C1")]


def test_heartbeat_throttles_idle_writes_but_publishes_transitions(monkeypatch):
    from types import SimpleNamespace

    now = [100.0]
    monkeypatch.setattr(
        chat_receive_status,
        "time",
        SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]),
    )
    writes = []
    original = chat_receive_status.atomic_write_text

    def write(path, text):
        writes.append(json.loads(text))
        original(path, text)

    monkeypatch.setattr(chat_receive_status, "atomic_write_text", write)
    status = ChatReceiveStatus()
    for second in range(10):
        now[0] = 100.0 + second
        status.save("slack", "aiko", "C1", state="ready")
    assert len(writes) == 2  # Once initially and once at the five-second refresh.
    status.save("slack", "aiko", "C1", state="catching_up")
    assert len(writes) == 3
    assert status.state("slack", "aiko", "C1") == "catching_up"
    status.save("slack", "aiko", "C1", state="ready")
    assert len(writes) == 4
    status.save("slack", "aiko", "C1", state="unavailable")
    assert not status.available("slack", "aiko", "C1")


@pytest.mark.parametrize("status", ["done", "asking", "blocked"])
@pytest.mark.parametrize(
    "action",
    [
        "chat_reply",
        "chat_post",
        "chat_reaction",
        "chat_noop",
        "git_commit",
        "chat_inspect",
        "issue_update",
        "git_push",
        *sorted(RunStore.TICKET_WRITE_EVIDENCE_TYPES),
    ],
)
def test_acknowledgement_requires_action_after_delivery(status, action):
    from guildbotics.capabilities.chat_batch import completed_chat_event_ids

    evidence = [
        {"evidence_type": "chat_batch", "payload": {"event_ids": ["E1"]}},
        {"evidence_type": "chat_updates", "payload": {"event_ids": ["E3"]}},
        {"evidence_type": action, "payload": {}},
        {"evidence_type": "chat_updates", "payload": {"event_ids": ["E5"]}},
    ]
    expected = (
        ["E1", "E3"]
        if status != "blocked"
        and action not in {"chat_noop", "git_commit", "chat_inspect"}
        else ["E1"]
    )
    assert completed_chat_event_ids(evidence, status) == expected
    assert chat_batch_event_ids(evidence) == ["E1", "E3", "E5"]
