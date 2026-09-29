"""The command's window to the host, as a brain inside the command's
environment reaches it, answered in the test's own process.

A turn's brain asks the host for its conversation and has the host write its
records; this double keeps the conversations in a real ledger at the test's
workspace and keeps the records, so a test reads both.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_runtime import turn
from guildbotics.intelligences.agent_runtime.host_client import (
    COMMAND_ENV,
    CommandFacts,
    CredentialEntry,
    Entry,
    EventEntry,
    HostClient,
)
from guildbotics.intelligences.agent_runtime.models import (
    AgentEvent,
    ConversationKey,
    ConversationRecord,
    ResumePolicy,
)
from guildbotics.intelligences.agent_runtime.store import ConversationStore

_RECORD = TypeAdapter(ConversationRecord)
_ENTRIES = TypeAdapter(list[Entry])


class WindowDouble(HostClient):
    """Answers the conversation ledger from ``workspace_root`` and keeps what
    is recorded, in order."""

    def __init__(self, workspace_root: Path) -> None:
        super().__init__("http://window.test/host", "window-token")
        self.conversations = ConversationStore(workspace_root)
        self.entries: list[Any] = []

    def call(self, name: str, **arguments: Any) -> Any:
        if name == "resolve":
            record = self.conversations.resolve(
                ConversationKey(**arguments["key"]),
                ResumePolicy(arguments["policy"]),
                model=arguments["model"],
            )
            return asdict(record)
        if name == "save":
            record = _RECORD.validate_python(arguments["record"])
            self.conversations.save(record)
            return asdict(record)
        if name == "mark_unhealthy":
            record = _RECORD.validate_python(arguments["record"])
            self.conversations.mark_unhealthy(record, arguments["reason"])
            return asdict(record)
        if name == "record":
            self.entries.extend(_ENTRIES.validate_python(arguments["entries"]))
            return None
        raise AssertionError(f"Unexpected host call: {name}")

    async def acall(self, name: str, **arguments: Any) -> Any:
        return self.call(name, **arguments)

    def events(self) -> list[AgentEvent]:
        """The turn events recorded, in order."""
        return [entry.event for entry in self.entries if isinstance(entry, EventEntry)]

    def credentials(self) -> list[CredentialEntry]:
        """What the turns proved of their tools' logins, in order."""
        return [entry for entry in self.entries if isinstance(entry, CredentialEntry)]


def enter_command(
    monkeypatch: pytest.MonkeyPatch,
    window: HostClient,
    *,
    person_id: str = "aiko",
    run_id: str = "command-run",
    work_kind: str = "",
    trace_id: str = "command-trace",
) -> None:
    """Run what follows as code inside a command's environment: the command
    is ``run_id`` of ``person_id``, and its window to the host is
    ``window``."""
    monkeypatch.setattr(turn, "command_window", lambda: window)
    monkeypatch.setenv(
        COMMAND_ENV,
        CommandFacts(
            person_id=person_id,
            run_id=run_id,
            work_kind=work_kind,
            trace_id=trace_id,
            access=CommandAccess(),
        ).dump(),
    )
