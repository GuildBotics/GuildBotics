from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import guildbotics
from guildbotics.app_api.trace_presentations import (
    normalize_trace_presentation,
    supports_trace_event,
)
from guildbotics.observability.session_transcripts import INDEX_EVENT_TYPES

_PACKAGE_ROOT = Path(guildbotics.__file__).parent
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z_]*(?:\.[a-z_]+)+$")


def _event(
    event_type: str,
    *,
    command: str = "",
    payload: dict[str, object] | None = None,
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "kind": "event",
        "type": event_type,
        "command": command,
        "payload": payload or {},
        "attributes": attributes or {},
    }


@pytest.mark.parametrize("phase", ["started", "finished"])
def test_command_lifecycle_uses_command_as_message(phase: str) -> None:
    presentation = normalize_trace_presentation(
        _event(
            f"member.command.{phase}",
            command="guildbotics member memory record",
            payload={"command": "guildbotics member memory record"},
        )
    )

    assert presentation.label_key.endswith(f"command_{phase}")
    assert presentation.message == "guildbotics member memory record"


def test_command_failure_prefers_failure_detail() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "command.failed",
            command="workflows/demo",
            payload={"message": "Command was cancelled."},
        )
    )

    assert presentation.message == "Command was cancelled."
    assert presentation.tone == "danger"


def test_deferred_command_has_an_informational_presentation() -> None:
    presentation = normalize_trace_presentation(
        _event("command.deferred", payload={"message": "Service unavailable."})
    )
    assert supports_trace_event("command.deferred")
    assert presentation.label_key.endswith("command_deferred")
    assert presentation.message == "Service unavailable."
    assert presentation.tone == "info"


def test_ticket_comment_uses_structured_target() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "ticket.comment",
            payload={"action": "commented", "ticket": {"label": "#313"}},
            attributes={"target.kind": "ticket", "target.label": "#313"},
        )
    )

    assert presentation.label_key.endswith("ticket_comment")
    assert presentation.message == "#313"
    assert presentation.message != "ticket.comment"


def test_github_target_label_falls_back_to_the_payload_entry() -> None:
    # A local PR is shown by the label its service gives it, not a "#n".
    presentation = normalize_trace_presentation(
        _event(
            "pull_request.item",
            payload={"action": "opened", "pull_request": {"label": "!3"}},
        )
    )

    assert presentation.message == "!3"


def test_repository_push_shows_the_branch_it_pushed() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "repository.push",
            payload={"ref": "refs/heads/ticket/T-1", "commits": []},
            attributes={"target.label": "#9"},
        )
    )

    assert presentation.message == "ticket/T-1"


def test_agent_and_span_payloads_are_normalized_in_app_api() -> None:
    assistant = normalize_trace_presentation(
        _event(
            "agent_runtime.assistant",
            payload={"name": "partial", "message": "Hello", "partial": True},
        )
    )
    span = normalize_trace_presentation(
        _event(
            "span.finished",
            payload={"model": "claude-code", "duration_ms": 24_100},
        )
    )
    with_effort = normalize_trace_presentation(
        _event(
            "span.finished",
            payload={
                "model": "claude-sonnet-5",
                "effort": "high",
                "duration_ms": 24_100,
            },
        )
    )

    assert assistant.label_key.endswith("assistant_partial")
    assert assistant.message == "Hello"
    assert span.message == "claude-code · 24.1s"
    assert with_effort.message == "claude-sonnet-5 · high · 24.1s"


def test_workflow_and_dispatch_events_have_meaningful_summaries() -> None:
    completed = normalize_trace_presentation(
        _event("workflow.completed", payload={"run_id": "run-1", "attempt": 2})
    )
    retry = normalize_trace_presentation(
        _event(
            "chat_dispatch.retry_scheduled",
            payload={
                "run_id": "run-1",
                "attempt_count": 2,
                "max_attempts": 3,
                "next_attempt_at": "2026-07-18T15:00:00+09:00",
            },
        )
    )

    assert completed.message_key.endswith("workflow_completed")
    assert completed.message_params == {
        "run": "run-1",
        "attempt": 2,
        "max_attempts": 0,
        "retry_at": "",
    }
    assert retry.message_key.endswith("chat_dispatch_retry_scheduled")
    assert retry.message_params["retry_at"] == "2026-07-18T15:00:00+09:00"


@pytest.mark.parametrize(
    "event_type",
    ["workflow.completion_missing", "chat_dispatch.abandoned"],
)
def test_terminal_workflow_failures_use_emitted_error(event_type: str) -> None:
    presentation = normalize_trace_presentation(
        _event(event_type, payload={"error": "Completion evidence was not recorded."})
    )

    assert presentation.message_key == ""
    assert presentation.message == "Completion evidence was not recorded."


@pytest.mark.parametrize(
    ("at", "hint", "suffix", "display"),
    [
        (
            "2026-10-03T15:30:12+09:00",
            "Resets in 25h57m34s",
            "_with_reset",
            "2026-10-03 15:30:12+09:00",
        ),
        ("2026-10-03T06:30:12Z", "", "_with_reset", "2026-10-03 15:30:12+09:00"),
        ("", "Resets in 25h57m34s", "_with_hint", "Resets in 25h57m34s"),
        ("", "", "", ""),
    ],
)
def test_rate_limit_summary_selects_reset_information(
    at, hint, suffix, display, local_rate_limit_timezone
):
    presentation = normalize_trace_presentation(
        _event(
            "workflow.rate_limited",
            payload={"retry_after_at": at, "retry_after_text": hint},
        )
    )
    assert (
        presentation.message_key
        == f"diagnostics.executions.messages.workflow_rate_limited{suffix}"
    )
    assert presentation.message_params["retry_at"] == display
    assert presentation.tone == "warning"


def test_info_log_preserves_neutral_badge_tone() -> None:
    presentation = normalize_trace_presentation(
        {"kind": "log", "level": "INFO", "message": "working"}
    )

    assert presentation.tone == "neutral"


def test_judgment_failure_shows_the_actual_conservative_route() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "decision.evaluated",
            payload={
                "brain": "chat_decision",
                "model": "jev-1.13.0",
                "route": "agent",
                "reason": "invalid",
                "error": "authentication_error",
            },
        )
    )
    assert presentation.label_key.endswith("decision_evaluated")
    assert (
        presentation.message
        == "chat_decision · jev-1.13.0 · agent · invalid · authentication_error"
    )
    assert presentation.tone == "warning"


def test_invalid_record_uses_explicit_unknown_fallback() -> None:
    presentation = normalize_trace_presentation(
        {"kind": "event", "type": "plugin.custom", "payload": "invalid"}
    )

    assert presentation.label_key.endswith("unknown")
    assert presentation.message == "plugin.custom"


def test_cli_agent_credential_failure_uses_agent_name_as_provider() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "credential.failed",
            payload={
                "provider": "cli_agent",
                "cli_agent": "claude-code",
                "code": "authentication",
            },
        )
    )

    assert presentation.message_params == {
        "provider": "claude-code",
        "code": "authentication",
    }


def test_github_scope_refusal_names_the_refused_target() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "target.scope_refused",
            payload={
                "target": "POST /repos/other/demo/issues",
                "scope_owner": "acme",
            },
        )
    )

    assert presentation.label_key.endswith("target_scope_refused")
    assert presentation.message == "POST /repos/other/demo/issues"
    assert presentation.tone == "danger"


def test_all_index_event_types_have_intentional_presentations() -> None:
    unsupported = sorted(
        event_type
        for event_type in INDEX_EVENT_TYPES
        if not supports_trace_event(event_type)
    )

    assert unsupported == []


def test_literal_diagnostics_emitters_have_intentional_presentations() -> None:
    """Fail when a new literal event is emitted without a display contract."""
    emitted = set()
    for path in _PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "event_type"
                for target in node.targets
            ):
                emitted.update(_string_literals(node.value))
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            candidate: ast.AST | None = None
            if name == "record_correlated_event":
                candidate = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "event_type"
                    ),
                    None,
                )
            elif name == "publish_event" and node.args:
                candidate = node.args[0]
            elif name == "_record_member_domain_event" and len(node.args) > 1:
                candidate = node.args[1]
            elif name == "_record_member_command_event" and node.args:
                candidate = node.args[0]
            if candidate is not None:
                emitted.update(_string_literals(candidate))

    unsupported = sorted(
        event_type for event_type in emitted if not supports_trace_event(event_type)
    )
    assert unsupported == []


def test_target_recorded_shows_the_title_it_declares() -> None:
    presentation = normalize_trace_presentation(
        _event(
            "target.recorded",
            payload={"pull_request": {"label": "#544"}},
            attributes={
                "target.kind": "pull_request",
                "target.label": "#544",
                "target.title": "診断ログの実行タイトル",
                "target.action": "inspected",
            },
        )
    )

    assert presentation.label_key.endswith("target_recorded")
    assert presentation.message == "#544 · 診断ログの実行タイトル"


@pytest.mark.parametrize(
    "event_type",
    [
        "repository.push",
        "pull_request.item",
        "ticket.item",
        "target.recorded",
        "ticket.comment",
        "credential.failed",
        "agent_runtime.process",
        "agent_runtime.turn",
        "agent_runtime.assistant",
        "agent_runtime.command",
        "agent_runtime.file_change",
        "agent_runtime.tool",
        "agent_runtime.approval",
        "agent_runtime.usage",
        "agent_runtime.failed",
        "agent_environment.network_egress_candidate",
        "scheduler.starting",
        "scheduler.stopping",
        "scheduler.stopped",
        "events.starting",
        "events.running",
        "events.stopping",
        "events.stopped",
        "events.failed",
        "system.started",
        "system.finished",
        "session.pointer",
        "chat.receive_state_reset",
    ],
)
def test_transcript_event_families_have_intentional_presentations(
    event_type: str,
) -> None:
    assert supports_trace_event(event_type)


def _call_name(value: ast.AST) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _string_literals(value: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(value)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and _EVENT_TYPE_RE.fullmatch(child.value)
    }


def test_network_egress_candidates_show_destinations_without_calling_them_denials() -> (
    None
):
    presentation = normalize_trace_presentation(
        _event(
            "agent_environment.network_egress_candidate",
            payload={
                "message": "indirect evidence",
                "candidates": [
                    {"destination": "blocked.example", "port": 443},
                    {"destination": "203.0.113.8"},
                ],
            },
        )
    )

    assert presentation.label_key.endswith("network_egress_candidate")
    assert presentation.message == "blocked.example:443, 203.0.113.8"
    assert presentation.tone == "info"


def test_reasoning_is_labelled_apart_from_the_reply() -> None:
    reasoning = normalize_trace_presentation(
        _event(
            "agent_runtime.assistant",
            payload={"name": "thinking", "message": "The user wants a path."},
        )
    )
    reply = normalize_trace_presentation(
        _event(
            "agent_runtime.assistant",
            payload={"name": "completed", "message": "Here it is."},
        )
    )

    assert reasoning.label_key.endswith("assistant_thinking")
    assert reasoning.message == "The user wants a path."
    assert reply.label_key.endswith("agentRuntime.assistant")


def _io(io_type: str, payload: dict) -> dict:
    return {"kind": "io", "type": io_type, "message": "", "payload": payload}


def test_a_request_record_surfaces_only_its_resolved_effort_level() -> None:
    presentation = normalize_trace_presentation(
        _io(
            "llm.request",
            {
                "prompt": "hello",
                "effort": {
                    "requested": "HIGH",
                    "resolved": "high",
                    "source": "runtime",
                    "applied_keys": ["reasoning_effort"],
                    "unsupported": False,
                },
            },
        )
    )

    assert presentation.effort == "high"


def test_a_record_that_imposed_no_effort_shows_none() -> None:
    assert (
        normalize_trace_presentation(_io("llm.request", {"prompt": "x"})).effort == ""
    )
    assert (
        normalize_trace_presentation(
            _io("cli_agent.request", {"effort": {"resolved": ""}})
        ).effort
        == ""
    )


def test_non_io_records_never_claim_an_effort() -> None:
    presentation = normalize_trace_presentation(
        _event("command.finished", command="workflows/demo")
    )
    assert presentation.effort == ""
