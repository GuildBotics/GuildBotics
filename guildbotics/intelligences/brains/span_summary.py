"""How a brain's span ends: recorded on the host, where the call ran or where
the command's environment sends it."""

from logging import Logger
from typing import Any

from guildbotics.observability.diagnostics_events import record_span_summary


def record_summary(
    logger: Logger,
    kind: str,
    slot: str,
    status: str,
    *,
    duration_ms: float | None,
    attributes: dict[str, Any],
    model: str = "",
    model_specified: bool | None = None,
    effort: str = "",
    usage: dict[str, Any] | None = None,
) -> None:
    """Close a brain's span with what it ran on, and log the one line it ends with.

    Only what is actually known is stated: a provider that named no model, or a
    turn that imposed no effort, leaves that term out rather than reporting the
    slot definition name as if it were the effective value.

    Args:
        logger: The brain's logger.
        kind: What the brain runs, as the log line names it.
        slot: The configured slot the brain runs, as the log line names it.
        status: How the span ended.
        duration_ms: How long the span's work took.
        attributes: What makes the span attributable without its model.
        model: The model the work really ran on, if known.
        model_specified: Whether the CLI turn requested a model, if applicable.
        effort: The effort the work really ran under, if known.
        usage: The token usage the provider reported.
    """
    record_span_summary(
        status=status,
        model=model,
        model_specified=model_specified,
        effort=effort,
        duration_ms=duration_ms,
        usage=usage,
        attributes=attributes,
    )
    parts = [f"model={model}"] if model else []
    if effort:
        parts.append(f"effort={effort}")
    if duration_ms is not None:
        parts.append(f"duration={duration_ms / 1000:.1f}s")
    logger.info(f"{kind} '{slot}' {status}: {' '.join(parts)}")
