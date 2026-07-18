"""Normalize raw diagnostics records into provider-neutral display contracts."""

from __future__ import annotations

from typing import Any

from guildbotics.app_api.models import TracePresentation

_COMMAND_PHASES = frozenset({"started", "finished", "failed"})
_SERVICE_PHASES = frozenset({"starting", "running", "stopping", "stopped", "failed"})
_MILLISECONDS_PER_SECOND = 1000
_AGENT_LABELS = {
    "process": "process",
    "turn": "turn",
    "assistant": "assistant",
    "command": "command",
    "file_change": "file_change",
    "tool": "tool",
    "approval": "approval",
    "usage": "usage",
    "failed": "failed",
}
_GITHUB_LABELS = {
    "github.push": "github_push",
    "github.pull_request": "github_pull_request",
    "github.issue": "github_issue",
    "github.issue_comment": "github_issue_comment",
}
_EXACT_EVENT_LABELS = {
    "scheduler.worker.failed": "scheduler_worker_failed",
    "workflow.completed": "workflow_completed",
    "workflow.completion_missing": "workflow_completion_missing",
    "workflow.rate_limited": "workflow_rate_limited",
    "chat_dispatch.retry_scheduled": "chat_dispatch_retry_scheduled",
    "chat_dispatch.abandoned": "chat_dispatch_abandoned",
    "credential.failed": "credential_failed",
    "diagnostics.completed": "diagnostics_completed",
    "verify.completed": "verify_completed",
    "system.started": "system_started",
    "system.finished": "system_finished",
    "session.pointer": "session_pointer",
    "chat.receive_state_reset": "chat_receive_state_reset",
}


def normalize_trace_presentation(item: dict[str, Any]) -> TracePresentation:
    """Return the only display interpretation consumed by the desktop UI."""
    kind = str(item.get("kind") or "")
    payload = _dict(item.get("payload"))
    attributes = _dict(item.get("attributes"))
    event_type = str(item.get("type") or "")
    message = str(item.get("message") or "")

    if kind == "log":
        level = str(item.get("level") or "LOG").upper()
        return _presentation(label=level, message=message, tone=_log_tone(level))
    if kind == "io":
        io_key = event_type.replace(".", "_")
        known = event_type in {
            "llm.request",
            "llm.response",
            "cli_agent.request",
            "cli_agent.response",
        }
        return _presentation(
            label_key=f"diagnostics.executions.ioTypes.{io_key}" if known else "",
            label=event_type or "I/O",
            message=message
            or _first_text(payload, "message", "prompt", "stdout")
            or event_type,
            tone="info",
        )
    if kind == "memory":
        action = str(
            attributes.get("memory.action") or event_type.removeprefix("memory.")
        )
        return _presentation(
            label_key=f"diagnostics.memory.actions.{action}" if action else "",
            label=action or "memory",
            message=message or event_type,
            tone=_memory_tone(action),
        )

    if event_type.startswith(("command.", "member.command.")):
        return _command_presentation(item, payload, event_type)
    if event_type == "scheduler.worker.failed":
        return _scheduler_worker_failure(payload)
    if event_type.startswith(("scheduler.", "events.")):
        return _service_presentation(payload, event_type)
    if event_type.startswith("agent_runtime."):
        return _agent_presentation(payload, event_type)
    if event_type.startswith("span."):
        return _span_presentation(payload, event_type)
    if event_type in _GITHUB_LABELS:
        return _github_presentation(payload, attributes, event_type)
    if event_type.startswith("workflow."):
        return _workflow_presentation(item, payload, event_type)
    if event_type.startswith("chat_dispatch."):
        return _chat_dispatch_presentation(payload, event_type)
    if event_type == "credential.failed":
        provider = _first_text(payload, "provider") or "provider"
        if provider == "cli_agent":
            provider = _first_text(payload, "cli_agent") or provider
        code = _first_text(payload, "code") or "authentication"
        return _presentation(
            label_key=_event_key("credential_failed"),
            label=event_type,
            message_key=_message_key("credential_failed"),
            message=code,
            params={"provider": provider, "code": code},
            tone="danger",
        )
    if event_type in {"diagnostics.completed", "verify.completed"}:
        return _diagnostics_presentation(payload, event_type)
    if event_type.startswith("system."):
        return _system_presentation(payload, attributes, event_type)
    if event_type == "session.pointer":
        path = _first_text(payload, "path") or str(attributes.get("session.path") or "")
        return _presentation(
            label_key=_event_key("session_pointer"),
            label=event_type,
            message=path or event_type,
        )
    if event_type == "chat.receive_state_reset":
        return _presentation(
            label_key=_event_key("chat_receive_state_reset"),
            label=event_type,
            message_key=_message_key("chat_receive_state_reset"),
            message=event_type,
            params={
                "members": payload.get("members", 0),
                "channels": payload.get("channels", 0),
            },
        )

    explicit = message or _first_text(payload, "message", "error", "code", "error_type")
    return _presentation(
        label_key=_event_key("unknown"),
        label=event_type or "Event",
        message=explicit or event_type,
        tone=_event_tone(event_type),
    )


def supports_trace_event(event_type: str) -> bool:
    """Whether an event type has an intentional, non-fallback presentation."""
    if event_type in _EXACT_EVENT_LABELS or event_type in _GITHUB_LABELS:
        return True
    if event_type.startswith(("command.", "member.command.")):
        return event_type.rsplit(".", 1)[-1] in _COMMAND_PHASES
    if event_type.startswith(("scheduler.", "events.")):
        return event_type.rsplit(".", 1)[-1] in _SERVICE_PHASES
    if event_type.startswith("agent_runtime."):
        return event_type.removeprefix("agent_runtime.") in _AGENT_LABELS
    if event_type.startswith("span."):
        return event_type.removeprefix("span.") in {"finished", "failed"}
    return False


def _command_presentation(
    item: dict[str, Any], payload: dict[str, Any], event_type: str
) -> TracePresentation:
    phase = event_type.rsplit(".", 1)[-1]
    command = str(item.get("command") or payload.get("command") or "")
    failure = _first_text(payload, "message", "error", "error_type", "code")
    return _presentation(
        label_key=_event_key(f"command_{phase}"),
        label=event_type,
        message=failure if phase == "failed" and failure else command or event_type,
        tone="danger" if phase == "failed" else "success",
    )


def _service_presentation(
    payload: dict[str, Any], event_type: str
) -> TracePresentation:
    target, phase = event_type.split(".", 1)
    failure = _first_text(payload, "error", "message")
    return _presentation(
        label_key=_event_key(
            "scheduler" if target == "scheduler" else "event_listener"
        ),
        label=event_type,
        message_key="" if failure else _message_key(f"{target}_{phase}"),
        message=failure or event_type,
        tone="danger"
        if phase == "failed"
        else "warning"
        if phase == "stopping"
        else "success"
        if phase in {"starting", "running"}
        else "neutral",
    )


def _scheduler_worker_failure(payload: dict[str, Any]) -> TracePresentation:
    return _presentation(
        label_key=_event_key("scheduler_worker_failed"),
        label="scheduler.worker.failed",
        message_key=_message_key("scheduler_worker_failed"),
        message="scheduler.worker.failed",
        params={
            "source": payload.get("source", ""),
            "count": payload.get("consecutive_errors", 0),
            "limit": payload.get("consecutive_error_limit", 0),
        },
        tone="danger",
    )


def _agent_presentation(payload: dict[str, Any], event_type: str) -> TracePresentation:
    subtype = event_type.removeprefix("agent_runtime.")
    label = _AGENT_LABELS.get(subtype, "")
    if subtype == "assistant" and payload.get("partial") is True:
        label = "assistant_partial"
    if subtype == "approval" and payload.get("name") == "policy":
        label = "approval_policy"
    message = _first_text(payload, "message")
    name = _first_text(payload, "name")
    return _presentation(
        label_key=f"diagnostics.executions.agentRuntime.{label}" if label else "",
        label=event_type,
        message_key=(
            f"diagnostics.executions.eventNames.{name}" if not message and name else ""
        ),
        message=message or name or event_type,
        tone="danger" if subtype == "failed" else "neutral",
    )


def _span_presentation(payload: dict[str, Any], event_type: str) -> TracePresentation:
    status = event_type.removeprefix("span.")
    model = _first_text(payload, "model")
    duration = payload.get("duration_ms")
    parts = [model]
    if isinstance(duration, int | float):
        parts.append(_format_duration(float(duration)))
    message = " · ".join(part for part in parts if part)
    return _presentation(
        label_key=f"diagnostics.executions.spanEvents.{status}",
        label=event_type,
        message=message or event_type,
        tone="danger" if status == "failed" else "success",
    )


def _github_presentation(
    payload: dict[str, Any], attributes: dict[str, Any], event_type: str
) -> TracePresentation:
    repo = str(attributes.get("github.repo") or "")
    number = str(
        attributes.get("github.number")
        or _nested(payload, "pull_request", "number")
        or _nested(payload, "issue", "number")
        or ""
    )
    title = str(
        _nested(payload, "pull_request", "title")
        or _nested(payload, "issue", "title")
        or payload.get("title")
        or ""
    )
    target = (
        f"{repo}#{number}"
        if repo and number
        else repo or (f"#{number}" if number else "")
    )
    if title:
        target = f"{target} · {title}" if target else title
    if event_type == "github.push":
        ref = str(payload.get("ref") or "").removeprefix("refs/heads/")
        target = " · ".join(part for part in (repo, ref) if part) or target
    return _presentation(
        label_key=_event_key(_GITHUB_LABELS[event_type]),
        label=event_type,
        message=target or _first_text(payload, "action") or event_type,
        tone="success",
    )


def _workflow_presentation(
    item: dict[str, Any], payload: dict[str, Any], event_type: str
) -> TracePresentation:
    label = _EXACT_EVENT_LABELS.get(event_type, "")
    error = _first_text(payload, "error", "message")
    command = str(item.get("command") or "")
    params = {
        "run": payload.get("run_id", ""),
        "attempt": payload.get("attempt", 0),
        "max_attempts": payload.get("max_attempts", 0),
        "retry_at": payload.get("retry_after_text")
        or payload.get("retry_after_at")
        or "",
    }
    message_name = label
    return _presentation(
        label_key=_event_key(label) if label else "",
        label=event_type,
        message_key=(
            _message_key(message_name)
            if message_name
            and not error
            and event_type != "workflow.completion_missing"
            else ""
        ),
        message=error or command or event_type,
        params=params,
        tone="danger"
        if event_type == "workflow.completion_missing"
        else "warning"
        if event_type == "workflow.rate_limited"
        else "success",
    )


def _chat_dispatch_presentation(
    payload: dict[str, Any], event_type: str
) -> TracePresentation:
    label = _EXACT_EVENT_LABELS.get(event_type, "")
    error = _first_text(payload, "error", "message")
    return _presentation(
        label_key=_event_key(label) if label else "",
        label=event_type,
        message_key=(
            _message_key(label)
            if label and not error and event_type != "chat_dispatch.abandoned"
            else ""
        ),
        message=error or event_type,
        params={
            "run": payload.get("run_id", ""),
            "attempt": payload.get("attempt_count", 0),
            "max_attempts": payload.get("max_attempts", 0),
            "retry_at": payload.get("next_attempt_at", ""),
        },
        tone="danger" if event_type.endswith(".abandoned") else "warning",
    )


def _diagnostics_presentation(
    payload: dict[str, Any], event_type: str
) -> TracePresentation:
    label = _EXACT_EVENT_LABELS[event_type]
    checks = payload.get("checks")
    count = len(checks) if isinstance(checks, list) else 0
    return _presentation(
        label_key=_event_key(label),
        label=event_type,
        message_key=_message_key(label),
        message=event_type,
        params={"count": count, "ok": bool(payload.get("ok"))},
        tone="success" if payload.get("ok") else "danger",
    )


def _system_presentation(
    payload: dict[str, Any], attributes: dict[str, Any], event_type: str
) -> TracePresentation:
    label = _EXACT_EVENT_LABELS.get(event_type, "")
    identifier = str(
        attributes.get("service_run_id")
        or attributes.get("system_session_id")
        or payload.get("path")
        or ""
    )
    return _presentation(
        label_key=_event_key(label) if label else "",
        label=event_type,
        message=identifier or event_type,
        tone="success" if event_type.endswith(".started") else "neutral",
    )


def _presentation(
    *,
    label: str,
    message: str,
    label_key: str = "",
    message_key: str = "",
    params: dict[str, Any] | None = None,
    tone: str = "neutral",
) -> TracePresentation:
    return TracePresentation(
        label_key=label_key,
        label_fallback=label,
        message_key=message_key,
        message=message,
        message_params=params or {},
        tone=tone,
    )


def _event_key(name: str) -> str:
    return f"diagnostics.executions.eventTypes.{name}"


def _message_key(name: str) -> str:
    return f"diagnostics.executions.messages.{name}"


def _first_text(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def _nested(value: dict[str, Any], key: str, nested_key: str) -> Any:
    nested = value.get(key)
    return nested.get(nested_key) if isinstance(nested, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _format_duration(ms: float) -> str:
    return (
        f"{max(0, round(ms))}ms"
        if ms < _MILLISECONDS_PER_SECOND
        else f"{ms / _MILLISECONDS_PER_SECOND:.1f}s"
    )


def _event_tone(event_type: str) -> str:
    if event_type.endswith(".failed"):
        return "danger"
    if any(part in event_type for part in ("running", "started", "finished")):
        return "success"
    if "stopping" in event_type:
        return "warning"
    return "neutral"


def _log_tone(level: str) -> str:
    if level in {"ERROR", "CRITICAL"}:
        return "danger"
    if level == "WARNING":
        return "warning"
    return "neutral"


def _memory_tone(action: str) -> str:
    if action == "record":
        return "success"
    if action in {"recall", "get", "update", "promote"}:
        return "info"
    if action == "archive":
        return "warning"
    return "neutral"
