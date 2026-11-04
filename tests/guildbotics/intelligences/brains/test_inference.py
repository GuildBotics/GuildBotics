from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from guildbotics.commands.errors import CommandError
from guildbotics.guest.window import WindowInference
from guildbotics.intelligences.brains.inference import AgnoCall, JevCall
from guildbotics.intelligences.effort import ResolvedEffort
from guildbotics.intelligences.agent_runtime.wire import HostCallError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "error"),
    [
        *(
            (method, HostCallError(category, "The host call timed out."))
            for method in ["agno", "jev"]
            for category in ["failed", "refused", "unavailable"]
        ),
        *(
            ("agno", HostCallError("failed", message, details))
            for message, details in [
                ("The call's result is too large.", {}),
                ("Host operation failed.", {}),
                (
                    "The inference call failed (ProviderError).",
                    {"error_type": "ProviderError"},
                ),
                (
                    "The inference call failed (ProviderError, reported status 429).",
                    {"error_type": "ProviderError", "status_code": 429},
                ),
            ]
        ),
    ],
)
async def test_window_inference_failure_contract(method, error):
    window = WindowInference(SimpleNamespace(acall=AsyncMock(side_effect=error)))
    call = (
        window.agno("aiko", _ask())
        if method == "agno"
        else window.jev(JevCall(state={}, questions={}, model="jev-latest"))
    )
    with pytest.raises(
        CommandError if error.category == "failed" else HostCallError
    ) as caught:
        await call
    assert str(caught.value) == str(error)
    assert (
        caught.value.__cause__ if error.category == "failed" else caught.value
    ) is error


def _ask() -> AgnoCall:
    return AgnoCall(
        brain="functions/answer",
        slot="default",
        effort=ResolvedEffort(),
        description="",
        message="hello",
    )
