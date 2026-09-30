"""Read and display one page of Dependabot alerts without an LLM."""

from __future__ import annotations

import json
from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.integrations.window import read_github
from guildbotics.intelligences.agent_runtime.host_client import command_window
from guildbotics.utils.i18n_tool import t

COMMAND_METADATA = {
    "description": {
        "en": "Read a page of Dependabot alerts or one alert; requires a ready execution environment.",
        "ja": "Dependabot アラートを1ページまたは個別に確認します。実行環境の準備が必要です。",
    },
    "read_only": True,
}


async def main(
    context: Any,
    repo: str,
    alert: str = "",
    state: str = "open",
    per_page: str = "30",
    continuation: str = "",
    output: str = "markdown",
) -> str:
    """Return JSON with output=json, or a readable page by default."""
    client = command_window()
    if client is None:
        raise CommandError(t("commands.github.security_alerts.environment"))
    if output not in {"json", "markdown"}:
        raise CommandError(t("commands.github.security_alerts.output"))
    try:
        page_size = int(per_page)
    except (ValueError, TypeError):
        raise CommandError(t("commands.github.security_alerts.page_size")) from None
    page = await read_github(
        client,
        context.person.person_id,
        "dependabot-alert" if alert else "dependabot-alerts",
        repo,
        identifier=str(alert),
        parameters={} if alert else {"state": state, "per_page": page_size},
        continuation=continuation,
    )
    items = [page["data"]] if alert else page["data"]
    result = {
        "repo": repo,
        "alerts": [_alert(item) for item in items],
        "continuation": page["continuation"],
    }
    return (
        json.dumps(result, ensure_ascii=False, indent=2)
        if output == "json"
        else _display(result)
    )


def _alert(item: dict[str, Any]) -> dict[str, Any]:
    dependency = item.get("dependency") or {}
    package = dependency.get("package") or {}
    advisory = item.get("security_advisory") or {}
    vulnerability = item.get("security_vulnerability") or {}
    patched = vulnerability.get("first_patched_version") or {}
    return {
        "number": item.get("number"),
        "url": item.get("html_url"),
        "state": item.get("state"),
        "package": package.get("name"),
        "ecosystem": package.get("ecosystem"),
        "manifest_path": dependency.get("manifest_path"),
        "severity": vulnerability.get("severity") or advisory.get("severity"),
        "ghsa": advisory.get("ghsa_id"),
        "cve": advisory.get("cve_id"),
        "summary": advisory.get("summary"),
        "description": advisory.get("description"),
        "vulnerable_version_range": vulnerability.get("vulnerable_version_range"),
        "first_patched_version": patched.get("identifier"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def _display(result: dict[str, Any]) -> str:
    labels = {
        "url": t("commands.github.security_alerts.fields.url"),
        "state": t("commands.github.security_alerts.fields.state"),
        "package": t("commands.github.security_alerts.fields.package"),
        "ecosystem": t("commands.github.security_alerts.fields.ecosystem"),
        "manifest_path": t("commands.github.security_alerts.fields.manifest_path"),
        "severity": t("commands.github.security_alerts.fields.severity"),
        "ghsa": t("commands.github.security_alerts.fields.ghsa"),
        "cve": t("commands.github.security_alerts.fields.cve"),
        "summary": t("commands.github.security_alerts.fields.summary"),
        "description": t("commands.github.security_alerts.fields.description"),
        "vulnerable_version_range": t(
            "commands.github.security_alerts.fields.vulnerable_version_range"
        ),
        "first_patched_version": t(
            "commands.github.security_alerts.fields.first_patched_version"
        ),
        "created_at": t("commands.github.security_alerts.fields.created_at"),
        "updated_at": t("commands.github.security_alerts.fields.updated_at"),
    }
    lines = [t("commands.github.security_alerts.heading", repo=result["repo"])]
    if not result["alerts"]:
        lines.append(t("commands.github.security_alerts.empty"))
    for item in result["alerts"]:
        lines.append(f"\n## #{item['number']}")
        for field, label in labels.items():
            missing = (
                t("commands.github.security_alerts.no_patch")
                if field == "first_patched_version"
                else t("commands.github.security_alerts.missing")
            )
            lines.append(f"- **{label}**: {item[field] or missing}")
    if result["continuation"]:
        lines.extend(
            [
                "",
                t("commands.github.security_alerts.more"),
                f"`continuation={result['continuation']}`",
            ]
        )
    return "\n".join(lines)
