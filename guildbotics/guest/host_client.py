"""What a command's isolated environment asks of the host, from inside it.

A command runs in its microVM; what only the host holds -- the login a turn
is lent, the run record, the conversation ledger, the diagnostics -- it asks
the host for through the command's window (the member broker's
``/host/<call>`` route, answered under the command's grant), with the URL and
token it was started with (:data:`~guildbotics.intelligences.agent_runtime.wire.HOST_URL_ENV`,
:data:`~guildbotics.intelligences.agent_runtime.wire.HOST_TOKEN_ENV`). This
module is that client; the wire it speaks is
:mod:`guildbotics.intelligences.agent_runtime.wire`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import asdict, fields
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter

from guildbotics.intelligences.agent_runtime.models import (
    AgentRuntimeError,
    AgentRuntimeErrorCategory,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.wire import (
    HOST_TOKEN_ENV,
    HOST_URL_ENV,
    CommandFacts,
    HostCallError,
    HostTurn,
)

#: How long a call may take: the host gives it as long as a member command,
#: and answers a call it gave up on.
_CALL_SECONDS = 330.0

_RECORD = TypeAdapter(ConversationRecord)


class HostClient:
    """The command's window to the host, at ``url`` with ``token``."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    def call(self, name: str, **arguments: Any) -> Any:
        """Ask the host for ``name`` and wait for its answer.

        Raises:
            AgentRuntimeError: When the host failed it with a turn's error.
            HostCallError: When the host refused it, failed it otherwise, or
                was not there to ask.
        """
        try:
            response = httpx.post(
                f"{self._url}/{name}",
                json=arguments,
                headers=self._headers,
                timeout=_CALL_SECONDS,
            )
        except httpx.TransportError as exc:
            raise HostCallError("unavailable", str(exc)) from exc
        return _result(response)

    async def acall(self, name: str, **arguments: Any) -> Any:
        """:meth:`call`, without holding the event loop."""
        try:
            async with httpx.AsyncClient(timeout=_CALL_SECONDS) as client:
                response = await client.post(
                    f"{self._url}/{name}", json=arguments, headers=self._headers
                )
        except httpx.TransportError as exc:
            raise HostCallError("unavailable", str(exc)) from exc
        return _result(response)

    async def begin_turn(
        self, tool: str, cwd: str, *, participant_labels: str = ""
    ) -> HostTurn:
        """Start a turn of ``tool`` working in ``cwd``, doing the command's
        work; the host lends the turn its login."""
        answer = await self.acall(
            "begin_turn",
            tool=tool,
            cwd=cwd,
            participant_labels=participant_labels,
        )
        return HostTurn(**answer)

    async def end_turn(self, turn_grant: str) -> str:
        """End the turn; why the login it was lent could not be used, if so."""
        return str((await self.acall("end_turn", turn_grant=turn_grant))["refusal"])

    async def record(self, entries: Sequence[BaseModel]) -> None:
        """Have the host write ``entries``, in order, in the command's trace."""
        await self.acall(
            "record", entries=[entry.model_dump(mode="json") for entry in entries]
        )


def command_window() -> HostClient | None:
    """The command's window to the host, when this process runs inside the
    command's isolated environment; none on the host."""
    url = os.environ.get(HOST_URL_ENV)
    return HostClient(url, os.environ[HOST_TOKEN_ENV]) if url else None


class ClientRunLedger:
    """The command's run record, through the host (a ``RunLedger``)."""

    def __init__(self, client: HostClient, facts: CommandFacts) -> None:
        self._client = client
        self.run_id = facts.run_id
        self.work_kind = facts.work_kind

    def require_completion(self) -> None:
        """Raise unless the run has recorded a terminal completion."""
        self._client.call("require_completion")

    def evidence(self) -> list[dict[str, Any]]:
        """Return the evidence the run has recorded so far."""
        return list(self._client.call("evidence"))

    def record_completed(self, attempt: int) -> None:
        """Record that the run's completion was found after an attempt."""
        self._client.call("record_completed", attempt=attempt)

    def record_completion_missing(
        self, attempt: int, max_attempts: int, error: str
    ) -> None:
        """Record an attempt that ended without the run's completion."""
        self._client.call(
            "record_completion_missing",
            attempt=attempt,
            max_attempts=max_attempts,
            error=error,
        )


class ClientConversationStore:
    """The conversation ledger, through the host.

    What the host saved comes back into the caller's record, as it does from
    the ledger itself.
    """

    def __init__(self, client: HostClient) -> None:
        self._client = client

    def resolve(
        self,
        key: ConversationKey,
        policy: ResumePolicy,
        *,
        model: str = "",
    ) -> ConversationRecord:
        answer = self._client.call(
            "resolve",
            key=asdict(key),
            policy=policy.value,
            model=model,
        )
        return _RECORD.validate_python(answer)

    def save(self, record: ConversationRecord) -> None:
        _adopt(record, self._client.call("save", record=_dump(record)))

    def mark_unhealthy(self, record: ConversationRecord, reason: str) -> None:
        _adopt(
            record,
            self._client.call("mark_unhealthy", record=_dump(record), reason=reason),
        )


def _dump(record: ConversationRecord) -> dict[str, Any]:
    return _RECORD.dump_python(record, mode="json")


def _adopt(record: ConversationRecord, answer: Any) -> None:
    saved = _RECORD.validate_python(answer)
    for each in fields(ConversationRecord):
        setattr(record, each.name, getattr(saved, each.name))


def _result(response: httpx.Response) -> Any:
    """What the host answered, or the error it answered with."""
    if response.status_code == httpx.codes.OK:
        return response.json()["result"]
    try:
        error = response.json()["error"]
    except (ValueError, KeyError, TypeError):
        raise HostCallError(
            "refused", f"The host refused the call ({response.status_code})."
        ) from None
    category = str(error.get("category") or "failed")
    message = str(error.get("message") or "")
    details = error.get("details") or {}
    if category in AgentRuntimeErrorCategory:
        raise AgentRuntimeError(
            AgentRuntimeErrorCategory(category), message, details=details
        )
    raise HostCallError(category, message, details)
