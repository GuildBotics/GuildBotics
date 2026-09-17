"""Status resolution for one execution (trace).

The defect these cover: a chat workflow decides with an LLM call before its
agent turn starts, so that call's ``span.finished`` used to turn the whole
still-running execution into "success".
"""

from __future__ import annotations

from typing import Any

from guildbotics.observability.trace_status import resolve_trace_status


def _event(event_type: str) -> dict[str, Any]:
    return {"kind": "event", "type": event_type}


def _log(level: str) -> dict[str, Any]:
    return {"kind": "log", "level": level, "message": "boom"}


def test_finished_llm_span_before_the_agent_turn_stays_running() -> None:
    assert (
        resolve_trace_status(
            [
                _event("command.started"),
                _event("span.finished"),
            ]
        )
        == "running"
    )


def test_command_boundary_finish_resolves_to_success() -> None:
    assert (
        resolve_trace_status(
            [
                _event("command.started"),
                _event("span.finished"),
                _event("command.finished"),
            ]
        )
        == "success"
    )


def test_trace_without_any_boundary_event_is_running_while_spans_finish() -> None:
    # Nothing here can speak for the whole trace, so the answer stays "work
    # has started" rather than claiming the execution is over.
    assert resolve_trace_status([_event("span.finished")]) == "running"


def test_interactive_and_diagnostics_completions_count_as_trace_completions() -> None:
    # These roots do not run a command, but each records its own end-of-trace
    # event, so they still resolve instead of hanging at "running".
    assert (
        resolve_trace_status(
            [_event("member.command.started"), _event("member.command.finished")]
        )
        == "success"
    )
    assert resolve_trace_status([_event("verify.completed")]) == "success"
    assert resolve_trace_status([_event("diagnostics.completed")]) == "success"


def test_failure_wins_over_any_later_completion_event() -> None:
    assert (
        resolve_trace_status([_event("span.failed"), _event("command.finished")])
        == "failed"
    )
    assert resolve_trace_status([_log("ERROR"), _event("command.finished")]) == "failed"


def test_workflow_completion_layer_overrides_the_command_boundary() -> None:
    finished = [_event("command.started"), _event("command.finished")]
    assert (
        resolve_trace_status([*finished, _event("workflow.completion_missing")])
        == "incomplete"
    )
    assert (
        resolve_trace_status(
            [
                *finished,
                _event("workflow.completion_missing"),
                _event("chat_dispatch.retry_scheduled"),
            ]
        )
        == "retry_scheduled"
    )
    assert (
        resolve_trace_status([*finished, _event("chat_dispatch.abandoned")])
        == "abandoned"
    )
    assert (
        resolve_trace_status([*finished, _event("workflow.rate_limited")])
        == "rate_limited"
    )


def test_trace_with_nothing_recorded_yet_is_info() -> None:
    assert resolve_trace_status([]) == "info"
    assert (
        resolve_trace_status([{"kind": "event", "type": "session.pointer"}]) == "info"
    )
