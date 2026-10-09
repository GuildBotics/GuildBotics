"""Read and display one page of dependency vulnerability alerts without an LLM."""

from __future__ import annotations

import json
import shlex
from typing import Any

from pydantic_core import to_jsonable_python

from guildbotics.commands.errors import CommandError
from guildbotics.runtime.code_hosting_service import RepositoryReadError
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
    try:
        page = await context.get_code_hosting_service().read(
            "dependency_alerts",
            repo,
            identifier=str(alert),
            parameters={} if alert else {"state": state, "page_size": size},
            continuation=continuation,
        )
    except RepositoryReadError as exc:
        # An anticipated read failure carries its guidance to the user.
        raise CommandError(str(exc)) from exc
    result = {
        "repo": repo,
        "alerts": to_jsonable_python(page.items),
        "continuation": page.continuation,
    }
    return (
        json.dumps(result, ensure_ascii=False, indent=2)
        if output == "json"
        else _display(result, context.person.person_id, alert, state, size)
    )


def _display(
    result: dict[str, Any], person: str, alert: str, state: str, page_size: int
) -> str:
    labels = {
        "url": t("commands.repository.security_alerts.fields.url"),
        "state": t("commands.repository.security_alerts.fields.state"),
        "package": t("commands.repository.security_alerts.fields.package"),
        "ecosystem": t("commands.repository.security_alerts.fields.ecosystem"),
        "manifest_path": t("commands.repository.security_alerts.fields.manifest_path"),
        "severity": t("commands.repository.security_alerts.fields.severity"),
        "identifiers": t("commands.repository.security_alerts.fields.identifiers"),
        "summary": t("commands.repository.security_alerts.fields.summary"),
        "affected_versions": t(
            "commands.repository.security_alerts.fields.affected_versions"
        ),
        "patched_version": t(
            "commands.repository.security_alerts.fields.patched_version"
        ),
        "created_at": t("commands.repository.security_alerts.fields.created_at"),
        "updated_at": t("commands.repository.security_alerts.fields.updated_at"),
    }
    heading = (
        t(
            "commands.repository.security_alerts.detail_heading",
            repo=_text(result["repo"]),
            alert=_text(alert),
        )
        if alert
        else t(
            "commands.repository.security_alerts.heading",
            repo=_text(result["repo"]),
            state=_text(state),
        )
    )
    lines = [heading]
    if not result["alerts"]:
        lines.append(t("commands.repository.security_alerts.empty", state=_text(state)))
    for item in result["alerts"]:
        title = " · ".join(
            _text(item[key]) for key in ("id", "package", "severity") if item[key]
        )
        lines.append(f"\n## {title}")
        for field, label in labels.items():
            value = item[field]
            if field == "identifiers":
                value = ", ".join(
                    f"{entry['type']}: {entry['value']}" for entry in value
                )
            if value or field == "patched_version":
                lines.append(
                    f"- **{label}**: {_text(value) if value else t('commands.repository.security_alerts.no_patch')}"
                )
        if alert and item["description"]:
            lines.extend(
                [
                    "",
                    f"**{t('commands.repository.security_alerts.fields.description')}**",
                    "",
                ]
            )
            lines.extend("> " + line for line in item["description"].splitlines())
    if result["continuation"]:
        lines.extend(
            [
                "",
                t("commands.repository.security_alerts.more"),
                "```sh",
                shlex.join(
                    [
                        "guildbotics",
                        "run",
                        "repository/security_alerts",
                        "--person",
                        person,
                        f"repo={result['repo']}",
                        f"state={state}",
                        f"page_size={page_size}",
                        f"continuation={result['continuation']}",
                        "output=markdown",
                    ]
                ),
                "```",
            ]
        )
    return "\n".join(lines)


def _text(value: Any) -> str:
    """Keep scalar values readable on one line in command output."""
    return " ".join(str(value).split())
