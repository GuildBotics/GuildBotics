from __future__ import annotations

from typing import Any

from guildbotics.entities.message import Message
from guildbotics.intelligences.functions import talk_as

COMMAND_METADATA = {
    "description": "Check the member's model through the inference gateway.",
    "read_only": True,
    "requires": ["llm"],
}


async def main(context: Any) -> str:
    """Exercise the same model slot and request as the scenario check."""
    return await talk_as(
        context,
        "Reply with exactly OK.",
        "diagnostics",
        [
            Message(
                content="Reply with exactly OK.",
                author="User",
                author_type=Message.USER,
                timestamp="",
            )
        ],
    )
