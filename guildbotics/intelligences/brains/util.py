import json
import textwrap
from typing import Type

import jinja2
from pydantic import BaseModel

from guildbotics.utils.text_utils import get_json_str


def to_header(title: str) -> str:
    """Format a title as a header."""
    line = "-" * 3
    return f"{line}\n\n# {title}\n\n{line}\n"


def to_plain_text(
    description: str | None,
    user_input: str | None,
    response_class: Type[BaseModel] | None = None,
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
    raw_output: str | Type[BaseModel], response_class: Type[BaseModel]
) -> BaseModel | str:
    """Convert raw output to a response class."""
    if isinstance(raw_output, response_class):
        return raw_output

    json_str = get_json_str(str(raw_output))
    try:
        return response_class.model_validate_json(json_str)
    except Exception:
        return json_str


def _replace_placeholders(
    text: str, placeholders: dict[str, str], placeholder: str
) -> str:
    for key, value in placeholders.items():
        var_name = placeholder.format(key)
        if var_name in text:
            text = text.replace(var_name, str(value))

    return text


def replace_placeholders_by_default(text: str, placeholders: dict[str, str]) -> str:
    text = _replace_placeholders(text, placeholders, "{{{{{}}}}}")
    text = _replace_placeholders(text, placeholders, "${{{}}}")
    text = _replace_placeholders(text, placeholders, "{{{}}}")
    return text


def replace_placeholders_by_jinja2(text: str, placeholders: dict[str, str]) -> str:
    template = jinja2.Template(text)
    return template.render(**placeholders)


def replace_placeholders(
    text: str, placeholders: dict[str, str], template_engine: str = "default"
) -> str:
    if template_engine == "jinja2":
        return replace_placeholders_by_jinja2(text, placeholders)
    else:
        return replace_placeholders_by_default(text, placeholders)
