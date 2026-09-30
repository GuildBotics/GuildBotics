"""Read and display one page of dependency vulnerability alerts without an LLM."""

from __future__ import annotations

import json
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.utils.i18n_tool import t

COMMAND_METADATA = {
    "description": {
        "en": "Read a page of dependency vulnerability alerts or one alert; requires a ready execution environment.",
        "ja": "依存ライブラリの脆弱性アラートを1ページまたは個別に確認します。実行環境の準備が必要です。",
    },
    "read_only": True,
}


async def main(
    context: Any,
    repo: str,
    alert: str = "",
    state: str = "open",
    page_size: str = "30",
    continuation: str = "",
    output: str = "markdown",
) -> str:
    """Return JSON with output=json, or a readable page by default."""
    if output not in {"json", "markdown"}:
        raise CommandError(t("commands.repository.security_alerts.output"))
    try:
        size = int(page_size)
    except (ValueError, TypeError):
        raise CommandError(t("commands.repository.security_alerts.page_size")) from None
    page = await context.get_code_hosting_service().read(
        "dependency_alerts",
        repo,
        identifier=str(alert),
        parameters={} if alert else {"state": state, "page_size": size},
        continuation=continuation,
    )
    result = {
        "repo": repo,
        "alerts": [item.model_dump() for item in page.items],
        "continuation": page.continuation,
    }
    return (
        json.dumps(result, ensure_ascii=False, indent=2)
        if output == "json"
        else _display(result)
    )


def _display(result: dict[str, Any]) -> str:
    labels = {
        "url": t("commands.repository.security_alerts.fields.url"),
        "state": t("commands.repository.security_alerts.fields.state"),
        "package": t("commands.repository.security_alerts.fields.package"),
        "ecosystem": t("commands.repository.security_alerts.fields.ecosystem"),
        "manifest_path": t("commands.repository.security_alerts.fields.manifest_path"),
        "severity": t("commands.repository.security_alerts.fields.severity"),
        "identifiers": t("commands.repository.security_alerts.fields.identifiers"),
        "summary": t("commands.repository.security_alerts.fields.summary"),
        "description": t("commands.repository.security_alerts.fields.description"),
        "affected_versions": t(
            "commands.repository.security_alerts.fields.affected_versions"
        ),
        "patched_version": t(
            "commands.repository.security_alerts.fields.patched_version"
        ),
        "created_at": t("commands.repository.security_alerts.fields.created_at"),
        "updated_at": t("commands.repository.security_alerts.fields.updated_at"),
    }
    lines = [t("commands.repository.security_alerts.heading", repo=result["repo"])]
    if not result["alerts"]:
        lines.append(t("commands.repository.security_alerts.empty"))
    for item in result["alerts"]:
        lines.append(f"\n## {item['id']}")
        for field, label in labels.items():
            missing = (
                t("commands.repository.security_alerts.no_patch")
                if field == "patched_version"
                else t("commands.repository.security_alerts.missing")
            )
            value = item[field]
            if field == "identifiers":
                value = ", ".join(
                    f"{entry['type']}: {entry['value']}" for entry in value
                )
            lines.append(f"- **{label}**: {value or missing}")
    if result["continuation"]:
        lines.extend(
            [
                "",
                t("commands.repository.security_alerts.more"),
                f"`continuation={result['continuation']}`",
            ]
        )
    return "\n".join(lines)
