"""Every chat of the factory keeps the same contract.

The same steps run against each provider: the member's identity, a channel by
its name, a post and a reply that read back in their channel and thread in the
order they were written, a reaction that a second attempt does not repeat, a
message URL that names its message and thread, a listener that hears what a
person writes after it starts and nothing once it stops, and the member's
credentials.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
import pytest_asyncio

from guildbotics.integrations.factory import PROVIDERS, ServiceIntegrationFactory
from guildbotics.runtime.chat_service import (
    ChatEvent,
    ChatService,
    ChatServiceError,
    ChatThreadNotFoundError,
)
from tests.guildbotics.integrations.contracts.chat_providers import (
    CHANNEL_NAME,
    ChatHarness,
    chat_harness,
)

#: The port's operations, each of which every chat answers.
_OPERATIONS = sorted(
    name
    for name, member in vars(ChatService).items()
    if getattr(member, "__isabstractmethod__", False)
)


@pytest_asyncio.fixture(params=sorted(name for name, p in PROVIDERS.items() if p.chat))
async def chat(request, monkeypatch):
    provided = chat_harness(request.param, monkeypatch)
    yield provided
    await provided.aclose()


def test_every_chat_answers_every_operation_of_the_port(chat: ChatHarness):
    implementation = type(chat.service)
    assert [
        operation
        for operation in _OPERATIONS
        if getattr(implementation, operation) is getattr(ChatService, operation)
    ] == []


@pytest.mark.asyncio
async def test_what_the_member_writes_reads_back_in_order(chat: ChatHarness):
    service = chat.service
    identity = await service.get_bot_identity()
    channel_id = await service.resolve_channel_id(CHANNEL_NAME)
    missing = await service.resolve_channel_id("no-such-channel")

    posted = await service.post_message(channel_id, "Good morning!")
    replied = await service.post_message(
        channel_id, "Details follow.", thread_id=posted.message_id
    )
    channel = await service.list_channel_events(channel_id)
    thread = await service.list_thread_events(channel_id, thread_id=posted.message_id)

    assert identity.user_id
    assert (channel_id, missing) == (chat.channel_id, None)
    assert (posted.channel_id, posted.thread_id) == (channel_id, posted.message_id)
    assert (replied.thread_id, replied.channel_id) == (posted.message_id, channel_id)
    assert replied.occurred_at >= posted.occurred_at
    # The channel lists the messages that start threads, a reply only in its
    # thread; a thread is its first message and its replies, in order.
    assert posted.message_id in [e.message_id for e in channel.events]
    assert replied.message_id not in [e.message_id for e in channel.events]
    assert [(e.message_id, e.text) for e in sorted(thread.events, key=_order)] == [
        (posted.message_id, "Good morning!"),
        (replied.message_id, "Details follow."),
    ]
    mine = next(e for e in thread.events if e.message_id == replied.message_id)
    assert mine.is_from_user(identity.user_id)
    assert mine.is_thread_reply and mine.thread_id == posted.message_id
    assert mine.occurred_at == replied.occurred_at
    assert mine.event_id == f"{channel_id}:{replied.message_id}"


@pytest.mark.asyncio
async def test_a_channel_window_holds_what_occurred_within_it(chat: ChatHarness):
    service = chat.service
    first = await service.post_message(chat.channel_id, "first")
    second = await service.post_message(chat.channel_id, "second")

    window = await service.list_channel_events(
        chat.channel_id, since=first.occurred_at, until=first.occurred_at
    )
    after = await service.list_channel_events(chat.channel_id, since=second.occurred_at)

    assert [e.message_id for e in window.events] == [first.message_id]
    assert [e.message_id for e in after.events] == [second.message_id]


@pytest.mark.asyncio
async def test_a_reaction_is_kept_once(chat: ChatHarness):
    message_id = chat.write("Please review.")
    identity = await chat.service.get_bot_identity()

    await chat.service.add_reaction(chat.channel_id, message_id, "ack")
    await chat.service.add_reaction(chat.channel_id, message_id, "ack")

    assert len(chat.reactions(message_id)) == 1
    with pytest.raises(ChatServiceError):
        await chat.service.add_reaction(chat.channel_id, message_id, "thumbsup")
    assert identity.user_id


@pytest.mark.asyncio
async def test_a_missing_thread_is_not_found(chat: ChatHarness):
    with pytest.raises(ChatThreadNotFoundError):
        await chat.service.list_thread_events(
            chat.channel_id, thread_id="1728999999.999999"
        )


def test_a_message_url_names_its_message_and_thread(chat: ChatHarness):
    root = chat.write("question")
    reply = chat.write("detail", thread_id=root)

    of_root = chat.service.parse_message_url(chat.url(root))
    of_reply = chat.service.parse_message_url(chat.url(reply, root))

    assert (of_root.channel_id, of_root.message_id, of_root.thread_id) == (
        chat.channel_id,
        root,
        root,
    )
    assert (of_reply.message_id, of_reply.thread_id) == (reply, root)
    with pytest.raises(ChatServiceError):
        chat.service.parse_message_url("https://example.com/not/a/message")


def test_the_listener_hears_what_is_written_while_it_runs(chat: ChatHarness):
    factory = ServiceIntegrationFactory()
    activity = threading.Event()
    key = factory.listener_key(chat.person, chat.team)
    listener = factory.create_event_listener(
        logging.getLogger(__name__), chat.team, [chat.person], activity.set
    )
    before = chat.write("before the listener")
    listener.start()
    received: list[ChatEvent] = []
    try:
        _wait(lambda: listener.connected)
        root = chat.write("@aiko please check")
        reply = chat.write("also this", thread_id=root)
        _wait(lambda: received.extend(listener.drain_events()) or len(received) >= 2)  # noqa: PLR2004
        assert activity.is_set()
    finally:
        listener.stop()
    late = chat.write("after the listener")
    time.sleep(0.2)

    assert isinstance(key, str)
    assert not listener.connected and not listener.auth_failed
    assert [(e.message_id, e.thread_id, e.text) for e in received] == [
        (root, root, "@aiko please check"),
        (reply, root, "also this"),
    ]
    assert received[0].channel_id == chat.channel_id
    assert before not in [e.message_id for e in listener.drain_events()]
    assert late not in [e.message_id for e in received]


@pytest.mark.asyncio
async def test_the_members_credentials_are_checked(chat: ChatHarness):
    assert await chat.service.check_credentials() == chat.credentials


def _order(event: ChatEvent) -> str:
    return event.position


def _wait(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)
