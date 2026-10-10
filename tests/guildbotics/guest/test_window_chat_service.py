"""Only a member command's reported failures belong to the chat contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from guildbotics.guest.window import MemberCommandError, WindowChatService
from guildbotics.intelligences.agent_runtime.wire import (
    HostCallError,
)
from guildbotics.runtime.chat_service import ChatServiceError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_bot_identity", ()),
        ("resolve_channel_id", ("general",)),
        ("post_message", ("C1", "hello")),
        ("add_reaction", ("C1", "1.0", "ack")),
    ],
)
@pytest.mark.parametrize("failure", ["reported", "defect", "refused"])
async def test_chat_failure_contract(method, args, failure):
    refused = HostCallError("refused", "grant refused")
    client = SimpleNamespace(
        acall=AsyncMock(
            return_value={
                "exit_code": 1,
                "stdout": "",
                "stderr": "Error: denied"
                if failure == "reported"
                else "Traceback: defect",
            },
            side_effect=refused if failure == "refused" else None,
        )
    )
    expected = {
        "reported": (ChatServiceError, "denied"),
        "defect": (MemberCommandError, "Traceback: defect"),
        "refused": (HostCallError, "grant refused"),
    }
    error_type, message = expected[failure]
    with pytest.raises(error_type, match=message):
        await getattr(WindowChatService(client, "aiko"), method)(*args)
