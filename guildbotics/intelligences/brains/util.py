import json
import textwrap

from pydantic import BaseModel

from guildbotics.utils.text_utils import get_json_str


def summary_log_line(
    kind: str,
    slot: str,
    status: str,
    *,
    duration_ms: float,
    model: str = "",
    effort: str = "",
) -> str:
    """Format the one line a brain logs when its span ends.

    Only what is actually known is stated: a provider that named no model, or a
    turn that imposed no effort, leaves that term out rather than reporting the
    slot definition name as if it were the effective value.
    """
    parts = [f"model={model}"] if model else []
    if effort:
        parts.append(f"effort={effort}")
    parts.append(f"duration={duration_ms / 1000:.1f}s")
    return f"{kind} '{slot}' {status}: {' '.join(parts)}"


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
