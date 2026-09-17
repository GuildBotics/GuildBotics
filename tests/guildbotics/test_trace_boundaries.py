"""Every route that opens a trace records the end of that trace.

A trace's status is resolved from the events its own opening layer records
(``TRACE_COMPLETED_EVENT_TYPES``): a child ``span.finished`` reports that one
provider call returned, so it can never say the execution behind it is over.
That rule only holds if the population of trace roots is complete -- a root
that records no completion event leaves its executions reading as still
running forever, and the chat dispatcher was exactly that root.

So the population is enumerated here rather than sampled. Every
``trace_scope(...)`` call site in the package is discovered and must be
declared with the completion event that route records. A new trace root fails
this test until its author answers the same question.
"""

from __future__ import annotations

import ast
from pathlib import Path

import guildbotics
from guildbotics.observability.event_types import TRACE_COMPLETED_EVENT_TYPES

PACKAGE_ROOT = Path(guildbotics.__file__).parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

#: ``(module path, enclosing function) -> completion event that route records``
TRACE_ROOTS: dict[tuple[str, str], str] = {
    # The Desktop assistant turn publishes its boundary on the event bus,
    # which records it into the same diagnostics store.
    ("guildbotics/app_api/runtime.py", "_assistant_turn"): "command.finished",
    # A Desktop command run; ``_run_command_traced`` publishes the boundary.
    ("guildbotics/app_api/runtime.py", "_run_reserved_command"): "command.finished",
    # Diagnostics runs do not run a command: each records its own end event.
    ("guildbotics/app_api/runtime.py", "verify"): "verify.completed",
    (
        "guildbotics/app_api/runtime.py",
        "run_scenario_diagnostics",
    ): "diagnostics.completed",
    # Slack-triggered chat workflows, through ``command_boundary``.
    (
        "guildbotics/drivers/pending_chat_dispatcher.py",
        "_dispatch",
    ): "command.finished",
    # Scheduled and routine commands, through ``run_with_logging``.
    (
        "guildbotics/drivers/task_scheduler.py",
        "_process_scheduled_tasks",
    ): "command.finished",
    (
        "guildbotics/drivers/task_scheduler.py",
        "_process_routine_tasks",
    ): "command.finished",
    ("guildbotics/drivers/task_scheduler.py", "_traced"): "command.finished",
    # Interactive member CLI sessions.
    ("guildbotics/cli/member.py", "_run_interactive"): "member.command.finished",
}


def _enclosing_function(node: ast.AST) -> str:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            return current.name
        current = getattr(current, "parent", None)
    return "<module>"


def _discover_trace_roots() -> set[tuple[str, str]]:
    roots: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent  # type: ignore[attr-defined]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "trace_scope":
                continue
            module = path.relative_to(REPOSITORY_ROOT).as_posix()
            roots.add((module, _enclosing_function(node)))
    return roots


def test_every_trace_root_is_declared_with_its_completion_event() -> None:
    discovered = _discover_trace_roots()
    undeclared = discovered - set(TRACE_ROOTS)
    stale = set(TRACE_ROOTS) - discovered
    assert not undeclared, (
        "these routes open a trace but do not declare how it ends: "
        f"{sorted(undeclared)}"
    )
    assert not stale, f"these declared trace roots no longer exist: {sorted(stale)}"


def test_declared_completion_events_can_end_a_trace() -> None:
    # A route may only claim an event the status resolver actually accepts as
    # the end of the whole trace.
    assert set(TRACE_ROOTS.values()) <= TRACE_COMPLETED_EVENT_TYPES
