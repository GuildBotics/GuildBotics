"""Member output formatting shared by the backend and its thin client."""

import json
import re
from typing import Any


def to_markdown(payload: dict[str, Any]) -> str:
    lines = []
    for key, value in payload.items():
        if key == "communication_style" and isinstance(value, dict):
            lines.append("## Communication Style")
            for style_key in (
                "active_member_instruction",
                "voice_basis",
                "interactive_replies",
                "comments",
                "neutral_documents",
                "machine_outputs",
            ):
                if style_key in value:
                    lines.append(f"- **{style_key}**: {value[style_key]}")
            continue
        if key == "capabilities" and isinstance(value, str):
            lines.append("## Member Capabilities")
            lines.append(value)
            continue
        if key == "failed_logs" and isinstance(value, list):
            lines.append("## Failed job logs")
            if not value:
                lines.append("_No failed CI job logs found._")
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "Unnamed job")
                conclusion = str(item.get("conclusion") or "unknown")
                lines.append(f"### {name} ({conclusion})")
                metadata = {
                    item_key: item_value
                    for item_key, item_value in item.items()
                    if item_key != "log"
                }
                lines.append(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
                log = str(item.get("log") or "")
                longest_ticks = max(
                    (len(match.group(0)) for match in re.finditer(r"`+", log)),
                    default=0,
                )
                fence = "`" * max(3, longest_ticks + 1)
                lines.extend((f"{fence}text", log.rstrip("\n"), fence))
            continue
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        lines.append(f"- **{key}**: {rendered}")
    return "\n".join(lines)
