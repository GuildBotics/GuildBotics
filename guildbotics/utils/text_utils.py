import re

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


def get_body_from_prompt(prompt: dict, args: list[str]) -> str:
    placeholders = {}

    for i, arg in enumerate(args, 1):
        kv = arg.split("=")
        if len(kv) > 1:
            placeholders[kv[0]] = kv[1]
        else:
            placeholders[f"{i}"] = str(arg)

    return replace_placeholders(
        prompt.get("body", "").strip(),
        placeholders,
        prompt.get("template_engine", "default"),
    )
