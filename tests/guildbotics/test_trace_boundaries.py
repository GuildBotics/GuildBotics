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

The same holds for *how* a run ends: a boundary that records the start of a
run has to record its end however the run ends. Cancellation is the case that
hides, because ``CancelledError`` and ``KeyboardInterrupt`` are not
``Exception``, so the functions that record both ends of a run are enumerated
here too and must catch ``BaseException``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import guildbotics
from guildbotics.observability.event_types import TRACE_COMPLETED_EVENT_TYPES

PACKAGE_ROOT = Path(guildbotics.__file__).parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent

#: ``(module path, enclosing function) -> completion event that route records``
TRACE_ROOTS: dict[tuple[str, str], str] = {
    # A command the Desktop runs, its assistants' included;
    # ``_run_command_traced`` publishes the boundary on the event bus, which
    # records it into the same diagnostics store.
    ("guildbotics/app_api/runtime.py", "_execute_command"): "command.finished",
    # Diagnostics runs do not run a command: each records its own end event.
    ("guildbotics/app_api/runtime.py", "verify"): "verify.completed",
    (
        "guildbotics/app_api/runtime.py",
        "run_scenario_diagnostics",
    ): "diagnostics.completed",
    # Selected Slack events, through ``command_boundary``; an event selection
    # declines before judgment opens no trace at all.
    (
        "guildbotics/drivers/pending_chat_dispatcher.py",
        "_select_and_run",
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
    # The ticket patrol: a dispatched ticket or a failed selection, through
    # ``run_with_logging``; an idle patrol opens no trace at all.
    ("guildbotics/drivers/task_scheduler.py", "_patrol_tickets"): "command.finished",
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


#: ``module path -> functions that record both ends of the boundary``. A trace
#: root delegates its boundary to one of these, so this is the population that
#: has to survive a cancelled run.
COMMAND_BOUNDARIES: set[tuple[str, str]] = {
    # Chat dispatch and the scheduler's commands, through ``run_with_logging``.
    ("guildbotics/drivers/utils.py", "command_boundary"),
    # A command the Desktop runs, its assistants' included.
    ("guildbotics/app_api/runtime.py", "_run_command_traced"),
    # An interactive member CLI command.
    ("guildbotics/cli/member.py", "_run_interactive"),
}

_EVENT_TYPE = re.compile(r"^[a-z_]+(?:\.[a-z_]+)+$")


def _records_event(node: ast.AST, suffix: str) -> bool:
    return any(
        isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and _EVENT_TYPE.fullmatch(child.value)
        and child.value.endswith(suffix)
        for child in ast.walk(node)
    )


def _discover_command_boundaries() -> set[tuple[str, str]]:
    boundaries: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _records_event(node, ".started") and _records_event(node, ".failed"):
                module = path.relative_to(REPOSITORY_ROOT).as_posix()
                boundaries.add((module, node.name))
    return boundaries


def _catches_base_exception(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(
        isinstance(name, ast.Name) and name.id == "BaseException" for name in names
    )


def test_every_command_boundary_is_declared() -> None:
    discovered = _discover_command_boundaries()
    assert discovered == COMMAND_BOUNDARIES, (
        "these functions record a start and a failure of the same run but are "
        f"not declared as command boundaries: {sorted(discovered ^ COMMAND_BOUNDARIES)}"
    )


def test_every_command_boundary_ends_a_cancelled_run() -> None:
    # A boundary that only catches ``Exception`` misses ``CancelledError`` and
    # ``KeyboardInterrupt``, which are how a stop ends the work it is draining.
    # Its ``*.started`` would then be the last record of the trace, so the
    # execution reads as running forever -- the very state this suite exists
    # to keep out.
    offenders = []
    for module, function_name in sorted(COMMAND_BOUNDARIES):
        tree = ast.parse((REPOSITORY_ROOT / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name != function_name:
                continue
            handlers = [
                handler
                for handler in ast.walk(node)
                if isinstance(handler, ast.ExceptHandler)
                and _records_event(handler, ".failed")
            ]
            if not any(_catches_base_exception(handler) for handler in handlers):
                offenders.append(f"{module}:{function_name}")
    assert not offenders, (
        "these boundaries leave a cancelled run without an end event: "
        f"{sorted(offenders)}"
    )


def test_no_command_boundary_has_work_after_its_failure_is_recorded() -> None:
    # A ``finally`` on the try that records the failure runs after both ends:
    # when it raises after a run that succeeded, neither ``*.finished`` nor
    # ``*.failed`` is recorded and the run reads as running forever. Cleanup is
    # part of the run, so it belongs in the try's body, where its failure is
    # the run's failure.
    offenders = []
    for module, function_name in sorted(COMMAND_BOUNDARIES):
        tree = ast.parse((REPOSITORY_ROOT / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if node.name != function_name:
                continue
            offenders.extend(
                f"{module}:{function_name}"
                for statement in ast.walk(node)
                if isinstance(statement, ast.Try)
                and statement.finalbody
                and any(_records_event(h, ".failed") for h in statement.handlers)
            )
    assert not offenders, (
        "these boundaries run cleanup after recording how the run ended: "
        f"{sorted(offenders)}"
    )
