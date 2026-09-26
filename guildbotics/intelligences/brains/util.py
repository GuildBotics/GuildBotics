import json
import textwrap
import time
from logging import Logger
from typing import Any

from pydantic import BaseModel

from guildbotics.observability.diagnostics_events import record_span_summary
from guildbotics.utils.text_utils import get_json_str


def record_summary(
    logger: Logger,
    kind: str,
    slot: str,
    status: str,
    *,
    started: float,
    attributes: dict[str, Any],
    model: str = "",
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
        started: ``time.monotonic()`` when the span's work began.
        attributes: What makes the span attributable without its model.
        model: The model the work really ran on, if known.
        effort: The effort the work really ran under, if known.
        usage: The token usage the provider reported.
    """
    duration_ms = (time.monotonic() - started) * 1000
    record_span_summary(
        status=status,
        model=model,
        effort=effort,
        duration_ms=duration_ms,
        usage=usage,
        attributes=attributes,
    )
    parts = [f"model={model}"] if model else []
    if effort:
        parts.append(f"effort={effort}")
    parts.append(f"duration={duration_ms / 1000:.1f}s")
    logger.info(f"{kind} '{slot}' {status}: {' '.join(parts)}")


def to_header(title: str) -> str:
    """Format a title as a header."""
    line = "-" * 3
    return f"{line}\n\n# {title}\n\n{line}\n"


def to_plain_text(
    description: str | None,
    user_input: str | None,
    response_class: type[BaseModel] | None = None,
) -> str:
    plain_text = ""

    if description:
        plain_text += f"{description}\n\n"

    if response_class:
        schema_dict = response_class.model_json_schema()
        plain_text += f"<{response_class.__name__} Schema>\n```json\n{json.dumps(schema_dict, indent=2)}\n```\n</{response_class.__name__} Schema>\n\n"

    if user_input:
        plain_text += f"<Conversation>\n{user_input}\n</Conversation>\n\n"

    return textwrap.dedent(plain_text).strip()


def to_response_class(
    raw_output: str | type[BaseModel], response_class: type[BaseModel]
) -> BaseModel | str:
    """Convert raw output to a response class."""
    if isinstance(raw_output, response_class):
        return raw_output

    json_str = get_json_str(str(raw_output))
    try:
        return response_class.model_validate_json(json_str)
    except Exception:
        return json_str
