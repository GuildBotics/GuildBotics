"""Where a brain's call to an external inference API goes.

A brain that calls an external API with a key (the agno model of a slot, Jev)
expands its template and reads the result wherever it runs, but the call
itself is made on the host, where the key is: the process has one way to it
(:func:`inference`). On the host that is the call itself
(:mod:`~guildbotics.intelligences.brains.inference_host`); inside a command's
isolated environment it is the command's window to the host, which answers it
under the command's grant.

What crosses is data only -- the expanded messages, the JSON Schema of the
output, the session state -- never a class to load or code to run: the
host builds nothing from what the environment names.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from guildbotics.commands.errors import CommandError
from guildbotics.intelligences.agent_runtime.host_client import (
    HostCallError,
    HostClient,
    command_window,
)
from guildbotics.intelligences.effort import ResolvedEffort
from guildbotics.observability import SpanContext
from guildbotics.utils.i18n_tool import t


class AgnoCall(BaseModel):
    """One request to the model of a member's slot.

    ``output_schema`` is the JSON Schema of the output the brain expects, if
    any; ``span`` the span the request is recorded under.
    """

    model_config = ConfigDict(extra="forbid")

    brain: str
    slot: str
    effort: ResolvedEffort
    description: str
    message: str
    output_schema: dict[str, Any] | None = None
    session_state: dict[str, Any] = {}
    span: SpanContext | None = None


class AgnoAnswer(BaseModel):
    """What the model answered, as JSON, and what it ran on."""

    content: Any
    model: str = ""
    usage: dict[str, Any] = {}


class JevCall(BaseModel):
    """One structured question to Jev."""

    model_config = ConfigDict(extra="forbid")

    state: Any
    questions: Any
    model: str


class Inference(Protocol):
    """The process's way to the external inference APIs."""

    async def agno(self, person_id: str, call: AgnoCall) -> AgnoAnswer:
        """Ask the model of ``person_id``'s slot ``call.slot``."""
        ...

    async def jev(self, call: JevCall) -> dict[str, Any]:
        """Ask Jev ``call``'s questions."""
        ...


def inference() -> Inference:
    """The process's way to the external inference APIs: the command's window
    inside its isolated environment, the calls themselves on the host."""
    window = command_window()
    if window is not None:
        return _Window(window)
    # Imported here: the environment never holds what the calls need.
    from guildbotics.intelligences.brains.inference_host import DirectInference

    return DirectInference()


class _Window:
    """The inference calls, answered by the host under the command's grant."""

    def __init__(self, client: HostClient) -> None:
        self._client = client

    async def agno(self, person_id: str, call: AgnoCall) -> AgnoAnswer:
        answer = await self._call(
            "agno",
            person_id=person_id,
            # A value JSON cannot carry reaches the prompt as its text.
            call=call.model_dump(mode="json", fallback=str),
        )
        return AgnoAnswer.model_validate(answer)

    async def jev(self, call: JevCall) -> dict[str, Any]:
        return dict(await self._call("jev", call=call.model_dump(mode="json")))

    async def _call(self, name: str, **arguments: Any) -> Any:
        """Report inference failures using only the host's credential-free details.

        Raises:
            CommandError: If inference failed on the host.
            HostCallError: If the window refused the call or is unavailable.
        """
        try:
            return await self._client.acall(name, **arguments)
        except HostCallError as exc:
            if exc.category != "failed":
                raise
            status = exc.details.get("status_code")
            raise CommandError(
                t(
                    "intelligences.inference.failed",
                    error_type=exc.details.get("error_type", type(exc).__name__),
                    status=f" (HTTP {status})" if status else "",
                )
            ) from exc
