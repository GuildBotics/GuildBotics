"""The workspace settings a command's access contract is read from, and the
command a test runs turns in, as a test states them."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_runtime import environment
from guildbotics.intelligences.agent_runtime.host_window import HostWindow
from guildbotics.intelligences.agent_runtime.member_broker import HostCalls
from guildbotics.utils.fileio import get_member_clone_path, get_workspace_root


def command_at(
    cwd: Path,
    tools: Iterable[str],
    access: CommandAccess = CommandAccess(),
    *,
    person_id: str = "aiko",
    host: HostCalls | None = None,
    inputs: Path | None = None,
) -> AbstractAsyncContextManager[None]:
    """The environment of a command of ``person_id``, configured with
    ``tools`` and declaring ``access``, working in ``cwd`` of the test's
    workspace, whose microVM's calls ``host`` answers (a grant for a run of
    its own, recording nowhere, by default)."""
    workspace_root = get_workspace_root()
    return environment.command_environment(
        access,
        frozenset(tools),
        cwd=cwd,
        workspace_root=workspace_root,
        clone=get_member_clone_path(person_id, workspace_root),
        inputs=inputs,
        host=host
        or HostWindow(
            person_id, "turn", "", workspace_root=workspace_root, ledger=Mock()
        ),
    )


def settle_contract(monkeypatch: pytest.MonkeyPatch, contract: AccessContract) -> None:
    """Make every command started from now on read ``contract``'s network and
    grants; whether it is read-only stays what the command declares."""
    monkeypatch.setattr(
        environment,
        "load_toolchain",
        lambda: SimpleNamespace(network=contract.network),
    )
    monkeypatch.setattr(environment, "resolve_access", lambda *_: contract.access)
