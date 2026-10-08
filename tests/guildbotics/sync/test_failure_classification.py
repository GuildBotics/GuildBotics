"""Which operations of a cycle fail as the hub's, and which as this device's.

The queue tells the two apart by the type of the failure, and only Git commands
that reach the hub raise :class:`HubGitError`. These tests tie that type to the
operations: every repository operation a cycle calls is classified here, the
hub ones are exactly those that run through ``_run_remote_git``, and that
function raises nothing but the hub's errors.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from types import ModuleType

import guildbotics.sync.commits as commits_module
import guildbotics.sync.local_repository as local_repository_module
import guildbotics.sync.manager as manager_module
from guildbotics.sync.local_repository import (
    HubCommandError,
    HubGitError,
    HubTimeoutError,
    LocalSyncRepository,
)

#: Operations that fail on this device: the working tree, the index, and the
#: local ``.git``. A failure here is shown as this device's to fix.
LOCAL = {
    "ahead_behind",
    "changed_paths",
    "commit",
    "has_remote",
    "head",
    "merge_base",
    "move_to",
    "read_entries",
    "read_staged",
    "rejected_id_for",
    "remote_head",
    "restore_from_index",
    "save_rejected",
    "stage_changes",
    "unstage",
    "verify_boundary",
}
#: Operations that reach the hub. A failure here is shown as the hub's, even
#: when its cause is in the local ``.git``: the operation decides.
HUB = {"fetch", "push"}


def _source_tree(module: ModuleType) -> ast.Module:
    return ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))


def _repository_calls(tree: ast.Module, receiver: str) -> set[str]:
    """Name every repository method ``tree`` reaches through ``receiver``."""
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if ast.unparse(node.value) != receiver:
            continue
        if callable(getattr(LocalSyncRepository, node.attr, None)):
            called.add(node.attr)
    return called


def _cycle_operations() -> set[str]:
    return _repository_calls(
        _source_tree(manager_module), "self._repository"
    ) | _repository_calls(_source_tree(commits_module), "repository")


def _methods_reaching_the_hub() -> set[str]:
    source = textwrap.dedent(inspect.getsource(LocalSyncRepository))
    (cls,) = ast.parse(source).body
    assert isinstance(cls, ast.ClassDef)
    return {
        method.name
        for method in cls.body
        if isinstance(method, ast.FunctionDef)
        and any(
            isinstance(node, ast.Name) and node.id == "_run_remote_git"
            for node in ast.walk(method)
        )
    }


def test_every_operation_a_cycle_calls_is_classified() -> None:
    """A new operation has to be named local or hub before the cycle uses it."""
    assert not LOCAL & HUB
    assert _cycle_operations() == LOCAL | HUB


def test_the_hub_operations_are_the_ones_that_reach_the_hub() -> None:
    """Calling an operation "hub" is only true when its failure is a hub error."""
    assert _cycle_operations() & _methods_reaching_the_hub() == HUB


def test_only_hub_errors_leave_the_command_that_reaches_the_hub() -> None:
    function = next(
        node
        for node in ast.walk(_source_tree(local_repository_module))
        if isinstance(node, ast.FunctionDef) and node.name == "_run_remote_git"
    )
    raised = {
        ast.unparse(node.exc.func if isinstance(node.exc, ast.Call) else node.exc)
        if node.exc is not None
        else "re-raise"
        for node in ast.walk(function)
        if isinstance(node, ast.Raise)
    }

    assert raised == {"HubTimeoutError", "HubCommandError"}
    assert issubclass(HubTimeoutError, HubGitError)
    assert issubclass(HubCommandError, HubGitError)
