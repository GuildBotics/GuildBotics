from __future__ import annotations

import logging

import httpx
import pytest

from guildbotics.integrations.slack.slack_chat_service import SlackChatService


def _client_for(handler):
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_get_bot_identity_uses_auth_test():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth.test")
        return httpx.Response(200, json={"ok": True, "user_id": "U1", "user": "bot"})

    client = _client_for(handler)
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    ident = await svc.get_bot_identity()
    assert ident.user_id == "U1"
    assert ident.display_name == "bot"
    await client.aclose()


@pytest.mark.asyncio
async def test_list_channel_events_normalizes_messages():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/conversations.history")
        body = {
            "ok": True,
            "messages": [
                {"type": "message", "user": "UUSER1", "text": "<@UBOT123> hi", "ts": "100.1"},
                {
                    "type": "message",
                    "subtype": "message_changed",
                    "text": "edited",
                    "ts": "100.15",
                },
                {
                    "type": "message",
                    "subtype": "bot_message",
                    "bot_id": "B1",
                    "text": "bot says",
                    "ts": "101.1",
                },
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "ts": "101.15",
                },
            ],
            "response_metadata": {"next_cursor": "abc"},
        }
        return httpx.Response(200, json=body)

    client = _client_for(handler)
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    page = await svc.list_channel_events("C1")
    assert page.cursor == "abc"
    assert len(page.events) == 2
    assert page.events[0].event_id == "C1:100.1"
    assert page.events[0].mentions == ["UBOT123"]
    assert page.events[0].is_thread_reply is False
    assert page.events[1].is_bot_message is True
    await client.aclose()


@pytest.mark.asyncio
async def test_resolve_channel_id_uses_conversations_list_and_caches():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith("/conversations.list")
        body = (await request.aread()).decode()
        if "cursor=" not in body:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "channels": [{"id": "C1", "name": "general"}],
                    "response_metadata": {"next_cursor": "next"},
                },
            )
        return httpx.Response(
            200,
            json={
                "ok": True,
                "channels": [{"id": "C2", "name": "dev-chat", "name_normalized": "dev-chat"}],
                "response_metadata": {"next_cursor": ""},
            },
        )

    client = _client_for(handler)
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    cid = await svc.resolve_channel_id("dev-chat")
    cid2 = await svc.resolve_channel_id("#dev-chat")
    assert cid == "C2"
    assert cid2 == "C2"
    assert calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_post_message_and_add_reaction_send_expected_payloads():
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = (await request.aread()).decode()
        seen.append((request.url.path, body))
        if request.url.path.endswith("/chat.postMessage"):
            return httpx.Response(200, json={"ok": True, "ts": "200.1"})
        if request.url.path.endswith("/reactions.add"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"ok": False, "error": "not_found"})

    client = _client_for(handler)
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    res = await svc.post_message("C1", "hello", thread_ts="100.1")
    await svc.add_reaction("C1", "100.1", "ack")
    assert res.message_ts == "200.1"
    assert res.thread_ts == "100.1"
    assert any(path.endswith("/chat.postMessage") and "thread_ts=100.1" in body for path, body in seen)
    assert any(
        path.endswith("/reactions.add") and "name=white_check_mark" in body
        for path, body in seen
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_add_reaction_raises_for_unknown_semantic_reaction():
    client = _client_for(lambda request: httpx.Response(200, json={"ok": True}))
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    with pytest.raises(RuntimeError, match="Unsupported semantic reaction"):
        await svc.add_reaction("C1", "100.1", "eyes")
    await client.aclose()


def test_normalize_and_render_participant_text_for_slack_mentions():
    svc = SlackChatService(
        logging.getLogger("test"),
        client=_client_for(lambda request: httpx.Response(200, json={"ok": True})),
        base_url="https://x.test",
    )
    participant_labels = {"UALICE": "alice", "UBOB": "bob"}

    normalized = svc.normalize_participant_text("<@UALICE> hi <@UBOB>", participant_labels)
    rendered = svc.render_participant_text("@alice hi @bob", participant_labels)

    assert normalized == "@alice hi @bob"
    assert rendered == "<@UALICE> hi <@UBOB>"


def test_render_participant_text_does_not_convert_ephemeral_labels():
    svc = SlackChatService(
        logging.getLogger("test"),
        client=_client_for(lambda request: httpx.Response(200, json={"ok": True})),
        base_url="https://x.test",
    )
    participant_labels = {
        "UALICE": "alice",
        "UUSER1": "user_1",
        "UAGENT1": "agent_1",
    }

    rendered = svc.render_participant_text("@alice @user_1 @agent_1", participant_labels)

    assert rendered == "<@UALICE> user_1 agent_1"


def test_render_participant_text_converts_hyphenated_person_labels():
    svc = SlackChatService(
        logging.getLogger("test"),
        client=_client_for(lambda request: httpx.Response(200, json={"ok": True})),
        base_url="https://x.test",
    )
    participant_labels = {
        "UMAINT": "maintenance-bot",
        "UALICE": "alice",
    }

    normalized = svc.normalize_participant_text(
        "<@UMAINT> hi <@UALICE>", participant_labels
    )
    rendered = svc.render_participant_text(
        "@maintenance-bot hi @alice", participant_labels
    )

    assert normalized == "@maintenance-bot hi @alice"
    assert rendered == "<@UMAINT> hi <@UALICE>"


@pytest.mark.asyncio
async def test_slack_api_error_raises_runtime_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    client = _client_for(handler)
    svc = SlackChatService(logging.getLogger("test"), client=client, base_url="https://x.test")
    with pytest.raises(RuntimeError, match="invalid_auth"):
        await svc.get_bot_identity()
    await client.aclose()
