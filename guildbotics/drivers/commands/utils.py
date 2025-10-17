from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from guildbotics.intelligences.functions import to_text


def stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return to_text(output)
    if isinstance(output, dict):
        return to_text(output)
    if isinstance(output, list):
        if output and isinstance(output[0], (BaseModel, dict)):
            return to_text(output)
        return "\n".join(str(item) for item in output)
    return str(output)
