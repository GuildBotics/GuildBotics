import pytest

from guildbotics.capabilities import member_chat
from guildbotics.capabilities.member_chat import MemberChatCapabilityService
from guildbotics.capabilities.member_github import MemberCapabilityError
from guildbotics.entities.team import Person, Project, Team
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatEventPage,
    ChatIdentity,
    ChatPostResult,
)


class FakeChatService:
    def __init__(self) -> None:
        self.identity = ChatIdentity(user_id="U_BOT", display_name="Aiko")
        self.channel_names = {"dev": "C_DEV"}
        self.posts: list[tuple[str, str, str | None]] = []
        self.reactions: list[tuple[str, str, str]] = []
        self.channel_inspects: list[tuple[str, str | None, str | None, int]] = []
        self.thread_inspects: list[tuple[str, str, int]] = []
        self.closed = False

    async def get_bot_identity(self):
        return self.identity

    async def resolve_channel_id(self, channel_name):
        return self.channel_names.get(channel_name)

    async def list_channel_events(
        self, channel_id, *, cursor=None, oldest_ts=None, latest_ts=None, limit=100
    ):
        del cursor
        self.channel_inspects.append((channel_id, oldest_ts, latest_ts, limit))
        return ChatEventPage(
            events=[
                ChatEvent(
                    event_id=f"{channel_id}:200.1",
                    channel_id=channel_id,
                    message_ts="200.1",
                    thread_ts="200.1",
                    author_id="U1",
                    text="newer",
                ),
                ChatEvent(
                    event_id=f"{channel_id}:100.1",
                    channel_id=channel_id,
                    message_ts="100.1",
                    thread_ts="100.1",
                    author_id="U2",
                    text="older",
                ),
            ],
            cursor="next",
        )

    async def list_thread_events(
        self, channel_id, *, thread_ts, cursor=None, limit=100
    ):
        del cursor
        self.thread_inspects.append((channel_id, thread_ts, limit))
        return ChatEventPage(
            events=[
                ChatEvent(
                    event_id=f"{channel_id}:100.2",
                    channel_id=channel_id,
                    message_ts="100.2",
                    thread_ts=thread_ts,
                    author_id="U2",
                    text="reply",
                    is_thread_reply=True,
                ),
                ChatEvent(
                    event_id=f"{channel_id}:100.1",
                    channel_id=channel_id,
                    message_ts="100.1",
                    thread_ts=thread_ts,
                    author_id="U1",
                    text="root",
                ),
            ]
        )

    async def post_message(self, channel_id, text, *, thread_ts=None):
        self.posts.append((channel_id, text, thread_ts))
        return ChatPostResult(
            channel_id=channel_id,
            message_ts="200.1",
            thread_ts=thread_ts or "200.1",
        )

    async def add_reaction(self, channel_id, message_ts, reaction):
        self.reactions.append((channel_id, message_ts, reaction))

    async def aclose(self):
        self.closed = True


def _service(chat_service=None):
    person = Person(person_id="aiko", name="Aiko")
    team = Team(project=Project(name="demo"), members=[person])
    logger = type("Logger", (), {"info": lambda *args, **kwargs: None})()
    return MemberChatCapabilityService(
        person, team, logger, chat_service or FakeChatService()
    )


@pytest.mark.asyncio
async def test_identity_returns_stable_member_payload():
    service = _service()

    result = await service.identity()

    assert result == {
        "service": "slack",
        "user_id": "U_BOT",
        "display_name": "Aiko",
        "person_id": "aiko",
    }


@pytest.mark.asyncio
async def test_post_resolves_channel_name_and_returns_evidence_payload():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.post(channel_id=None, channel_name="dev", body="hello team")

    assert fake.posts == [("C_DEV", "hello team", None)]
    assert result["channel_id"] == "C_DEV"
    assert result["message_ts"] == "200.1"
    assert result["thread_ts"] == "200.1"
    assert result["text"] == "hello team"
    assert result["posted"] is True


@pytest.mark.asyncio
async def test_inspect_channel_resolves_name_and_returns_messages_in_time_order():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.inspect_channel(
        channel_id=None,
        channel_name="dev",
        oldest_ts="100.0",
        latest_ts="300.0",
        limit=25,
    )

    assert fake.channel_inspects == [("C_DEV", "100.0", "300.0", 25)]
    assert result["mode"] == "channel"
    assert result["next_cursor"] == "next"
    assert [message["text"] for message in result["messages"]] == ["older", "newer"]


@pytest.mark.asyncio
async def test_inspect_thread_returns_thread_messages_in_time_order():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.inspect_thread(
        channel_id="C1", channel_name=None, thread_ts="100.1", limit=50
    )

    assert fake.thread_inspects == [("C1", "100.1", 50)]
    assert result["mode"] == "thread"
    assert result["thread_ts"] == "100.1"
    assert [message["text"] for message in result["messages"]] == ["root", "reply"]


@pytest.mark.asyncio
async def test_reply_posts_to_thread():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.reply(
        channel_id="C1", channel_name=None, thread_ts="100.1", body="reply"
    )

    assert fake.posts == [("C1", "reply", "100.1")]
    assert result["thread_ts"] == "100.1"
    assert result["text"] == "reply"


@pytest.mark.asyncio
async def test_reply_resolves_channel_name():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.reply(
        channel_id=None, channel_name="dev", thread_ts="100.1", body="reply"
    )

    assert fake.posts == [("C_DEV", "reply", "100.1")]
    assert result["channel_id"] == "C_DEV"


@pytest.mark.asyncio
async def test_reaction_add_restricts_to_semantic_reactions():
    fake = FakeChatService()
    service = _service(fake)

    result = await service.add_reaction(
        channel_id=None, channel_name="dev", message_ts="100.1", reaction="ack"
    )

    assert fake.reactions == [("C_DEV", "100.1", "ack")]
    assert result["channel_id"] == "C_DEV"
    assert result["reacted"] is True

    with pytest.raises(MemberCapabilityError):
        await service.add_reaction(
            channel_id="C1",
            channel_name=None,
            message_ts="100.1",
            reaction="white_check_mark",
        )


@pytest.mark.asyncio
async def test_check_credentials_reports_ok_for_valid_tokens(monkeypatch):
    monkeypatch.setenv("AIKO_SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("AIKO_SLACK_APP_TOKEN", "xapp-valid")

    probed: list[tuple[str, str | None]] = []

    async def fake_probe(app_token, base_url):
        probed.append((app_token, base_url))

    monkeypatch.setattr(member_chat, "probe_slack_app_token", fake_probe)
    service = _service()

    result = await service.check_credentials()

    assert result["status"] == "ok"
    assert result["bot_token"] == "ok"
    assert result["app_token"] == "ok"
    assert "bot_token_error" not in result
    assert "app_token_error" not in result
    assert probed == [("xapp-valid", None)]


@pytest.mark.asyncio
async def test_check_credentials_surfaces_invalid_app_token(monkeypatch):
    monkeypatch.setenv("AIKO_SLACK_BOT_TOKEN", "xoxb-valid")
    monkeypatch.setenv("AIKO_SLACK_APP_TOKEN", "xapp-broken")

    async def fake_probe(app_token, base_url):
        raise MemberCapabilityError("invalid_auth")

    monkeypatch.setattr(member_chat, "probe_slack_app_token", fake_probe)
    service = _service()

    result = await service.check_credentials()

    assert result["status"] == "failed"
    assert result["bot_token"] == "ok"
    assert result["app_token"] == "failed"
    assert result["app_token_error"] == "invalid_auth"


@pytest.mark.asyncio
async def test_check_credentials_validates_app_token_without_chat_service(monkeypatch):
    # A member with only an app token has no chat service (the factory requires a
    # bot token); the app-token probe must still run.
    monkeypatch.delenv("AIKO_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("AIKO_SLACK_APP_TOKEN", "xapp-valid")

    async def fake_probe(app_token, base_url):
        return None

    monkeypatch.setattr(member_chat, "probe_slack_app_token", fake_probe)
    person = Person(person_id="aiko", name="Aiko")
    team = Team(project=Project(name="demo"), members=[person])
    logger = type("Logger", (), {"info": lambda *args, **kwargs: None})()
    service = MemberChatCapabilityService(person, team, logger, None)

    result = await service.check_credentials()

    assert result["bot_token"] == "unconfigured"
    assert result["app_token"] == "ok"
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_check_credentials_reports_unconfigured_without_tokens(monkeypatch):
    monkeypatch.delenv("AIKO_SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AIKO_SLACK_APP_TOKEN", raising=False)
    service = _service()

    result = await service.check_credentials()

    assert result["status"] == "unconfigured"
    assert result["bot_token"] == "unconfigured"
    assert result["app_token"] == "unconfigured"


@pytest.mark.asyncio
async def test_safe_error_redacts_secret_markers():
    class FailingChatService(FakeChatService):
        async def get_bot_identity(self):
            raise RuntimeError("SLACK_BOT_TOKEN=xoxb-secret")

    service = _service(FailingChatService())

    with pytest.raises(MemberCapabilityError, match="safely") as exc_info:
        await service.identity()

    assert "xoxb-secret" not in str(exc_info.value)
