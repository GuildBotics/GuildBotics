import pytest

from guildbotics.capabilities.member_chat import MemberChatCapabilityService
from guildbotics.entities.team import Person
from guildbotics.integrations.local import chat
from guildbotics.integrations.local.chat import LocalChatService
from guildbotics.runtime.chat_service import ChatIdentity, CredentialCheck
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.runtime.member_invocation import (
    MemberInvocation,
    member_invocation_scope,
)
from tests.guildbotics.local_chat import at, lines, say

AIKO = Person(person_id="aiko", name="Aiko")


def _service(chat_service=None) -> MemberChatCapabilityService:
    """Aiko's chat, the local one, where ``dev`` is a channel."""
    if not chat.channel_path("dev").exists():
        say("dev", "welcome", message_id="0", occurred_at=at(0))
    return MemberChatCapabilityService(
        AIKO, chat_service or LocalChatService(AIKO), "local"
    )


def _posts(channel_id: str) -> list[tuple[str, str]]:
    return [
        (line["text"], line["thread_id"])
        for line in lines(channel_id)
        if line.get("author") == "aiko" and "reaction" not in line
    ]


@pytest.mark.asyncio
async def test_identity_returns_stable_member_payload():
    result = await _service().identity()

    assert result == {
        "service": "local",
        "user_id": "aiko",
        "display_name": "Aiko",
        "person_id": "aiko",
    }


@pytest.mark.asyncio
async def test_post_resolves_channel_name_and_returns_evidence_payload():
    result = await _service().post(
        channel_id=None, channel_name="dev", body="hello team"
    )

    [line] = [line for line in lines("dev") if line["author"] == "aiko"]
    assert _posts("dev") == [("hello team", line["message_id"])]
    assert result == {
        "service": "local",
        "channel_id": "dev",
        "message_id": line["message_id"],
        "thread_id": line["message_id"],
        "occurred_at": line["occurred_at"],
        "text": "hello team",
        "posted": True,
    }


@pytest.mark.asyncio
async def test_post_renders_participant_labels_from_workflow_context():
    with member_invocation_scope(
        MemberInvocation(participant_labels='{"u-bob":"bob","aiko":"aiko"}')
    ):
        result = await _service().post(
            channel_id="dev", channel_name=None, body="@bob please"
        )

    assert result["text"] == "@u-bob please"
    assert [text for text, _ in _posts("dev")] == ["@u-bob please"]


@pytest.mark.asyncio
async def test_inspect_channel_resolves_name_and_returns_messages_in_time_order():
    service = _service()
    say("dev", "newer", message_id="m2", occurred_at=at(200))
    say("dev", "older", message_id="m1", occurred_at=at(100))
    say("dev", "too late", message_id="m3", occurred_at=at(400))

    result = await service.inspect_channel(
        channel_id=None, channel_name="dev", since=at(100), until=at(300), limit=25
    )

    assert result["mode"] == "channel"
    assert result["since"] == at(100).isoformat()
    assert result["until"] == at(300).isoformat()
    assert [message["text"] for message in result["messages"]] == ["older", "newer"]
    assert result["messages"][0]["occurred_at"] == at(100).isoformat()


@pytest.mark.asyncio
async def test_inspect_thread_reads_by_id_or_by_message_url():
    service = _service()
    say("dev", "reply", message_id="r1", thread_id="t1", occurred_at=at(101))
    say("dev", "root", message_id="t1", occurred_at=at(100))

    by_id = await service.inspect_thread(
        channel_id="dev",
        channel_name=None,
        thread_id="t1",
        message_url=None,
        limit=50,
    )
    by_url = await service.inspect_thread(
        channel_id=None,
        channel_name=None,
        thread_id=None,
        message_url=chat.message_url("dev", "r1", "t1"),
        limit=50,
    )

    assert by_id["mode"] == "thread"
    assert by_id["thread_id"] == "t1"
    assert [m["text"] for m in by_id["messages"]] == ["root", "reply"]
    assert by_url["messages"] == by_id["messages"]


@pytest.mark.asyncio
async def test_inspect_thread_needs_a_thread():
    with pytest.raises(MemberCapabilityError):
        await _service().inspect_thread(
            channel_id="dev",
            channel_name=None,
            thread_id=None,
            message_url=None,
            limit=50,
        )


@pytest.mark.asyncio
async def test_reply_posts_to_the_thread_a_message_url_names():
    service = _service()
    say("dev", "root", message_id="t1", occurred_at=at(100))
    say("dev", "reply", message_id="r1", thread_id="t1", occurred_at=at(101))

    result = await service.reply(
        channel_id=None,
        channel_name=None,
        thread_id=None,
        message_url=chat.message_url("dev", "r1", "t1"),
        body="answer",
    )

    assert _posts("dev") == [("answer", "t1")]
    assert result["thread_id"] == "t1"
    assert result["text"] == "answer"


@pytest.mark.asyncio
async def test_reply_resolves_channel_name_and_renders_labels():
    with member_invocation_scope(
        MemberInvocation(participant_labels='{"u-bob":"bob"}')
    ):
        result = await _service().reply(
            channel_id=None,
            channel_name="dev",
            thread_id="0",
            message_url=None,
            body="@bob thoughts?",
        )

    assert _posts("dev") == [("@u-bob thoughts?", "0")]
    assert result["channel_id"] == "dev"


@pytest.mark.asyncio
async def test_reaction_add_restricts_to_semantic_reactions():
    service = _service()

    result = await service.add_reaction(
        channel_id=None, channel_name="dev", message_id="0", reaction="ack"
    )
    await service.add_reaction(
        channel_id="dev", channel_name=None, message_id="0", reaction="ack"
    )

    reactions = [line for line in lines("dev") if "reaction" in line]
    assert reactions == [{"reaction": "ack", "author": "aiko", "message_id": "0"}]
    assert result["message_id"] == "0"
    assert result["reacted"] is True

    with pytest.raises(MemberCapabilityError):
        await service.add_reaction(
            channel_id="dev",
            channel_name=None,
            message_id="0",
            reaction="white_check_mark",
        )


class _Credentials(LocalChatService):
    def __init__(self, *checks: CredentialCheck) -> None:
        super().__init__(AIKO)
        self._checks = list(checks)

    async def check_credentials(self) -> list[CredentialCheck]:
        return self._checks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checks", "status"),
    [
        ((), "ok"),
        (
            (
                CredentialCheck("bot_token", "ok"),
                CredentialCheck("app_token", "unconfigured"),
            ),
            "ok",
        ),
        (
            (
                CredentialCheck("bot_token", "unconfigured"),
                CredentialCheck("app_token", "unconfigured"),
            ),
            "unconfigured",
        ),
        (
            (
                CredentialCheck("bot_token", "ok"),
                CredentialCheck("app_token", "failed", "invalid_auth"),
            ),
            "failed",
        ),
    ],
)
async def test_check_credentials_reports_each_credential_and_the_whole(checks, status):
    result = await _service(_Credentials(*checks)).check_credentials()

    assert result["service"] == "local"
    assert result["status"] == status
    assert result["credentials"] == [
        {
            "name": check.name,
            "status": check.status,
            **({"error": check.error} if check.error else {}),
        }
        for check in checks
    ]


@pytest.mark.asyncio
async def test_safe_error_redacts_secret_markers():
    class FailingChatService(LocalChatService):
        async def get_bot_identity(self) -> ChatIdentity:
            raise RuntimeError("SLACK_BOT_TOKEN=xoxb-secret")

    service = _service(FailingChatService(AIKO))

    with pytest.raises(MemberCapabilityError, match="safely") as exc_info:
        await service.identity()

    assert "xoxb-secret" not in str(exc_info.value)
