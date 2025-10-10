import re
from typing import Any

import jinja2


def get_json_str(raw_output: str) -> str:
    # Try to find a fenced JSON block first
    match = re.search(r"```json\s*(\{[\s\S]*\})\s*```", raw_output)
    if match:
        json_str = match.group(1)
    else:
        # Fallback: extract first {…} JSON substring
        start = raw_output.find("{")
        end = raw_output.rfind("}") + 1
        if start != -1 and end != -1:
            json_str = raw_output[start:end]
        else:
            return raw_output.strip()
    return json_str.strip()


def _replace_placeholders(
    text: str, placeholders: dict[str, Any], placeholder: str
) -> str:
    for key, value in placeholders.items():
        var_name = placeholder.format(key)
        if var_name in text:
            text = text.replace(var_name, str(value))

    return text


def replace_placeholders_by_default(text: str, placeholders: dict[str, Any]) -> str:
    text = _replace_placeholders(text, placeholders, "{{{{{}}}}}")
    text = _replace_placeholders(text, placeholders, "${{{}}}")
    text = _replace_placeholders(text, placeholders, "{{{}}}")
    text = _replace_placeholders(text, placeholders, "${}")
    return text


def replace_placeholders_by_jinja2(text: str, placeholders: dict[str, Any]) -> str:
    template = jinja2.Template(text)
    return template.render(**placeholders)


def replace_placeholders(
    text: str, placeholders: dict[str, Any], template_engine: str = "default"
) -> str:
    if template_engine == "jinja2":
        return replace_placeholders_by_jinja2(text, placeholders)
    else:
        return replace_placeholders_by_default(text, placeholders)


def get_placeholders_from_args(
    args: list[str], add_index: bool = True
) -> dict[str, str]:
    placeholders = {}
    for i, arg in enumerate(args, 1):
        kv = arg.split("=", 1)
        if len(kv) > 1:
            placeholders[kv[0]] = kv[1]
        elif add_index:
            placeholders[f"arg{i}"] = str(arg)
            placeholders[f"{i}"] = str(arg)
    return placeholders


def get_body_from_prompt(prompt: dict, args: list[str]) -> str:
    template_engine = prompt.get("template_engine", "default")
    placeholders = get_placeholders_from_args(args)
    return replace_placeholders(
        prompt.get("body", "").strip(), placeholders, template_engine
    )
