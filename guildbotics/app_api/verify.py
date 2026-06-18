from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from dotenv import dotenv_values

from guildbotics.app_api.cli_agents import (
    resolve_cli_agent_path,
    resolve_default_cli_executable,
)
from guildbotics.app_api.models import ConfigStatus, VerifyCheck, VerifyResponse
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.github_utils import GitHubAppAuth
from guildbotics.utils.fileio import get_config_path, load_yaml_file

PROVIDER_ENV_KEYS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def resolve_default_model_provider() -> str:
    """Return the default LLM provider name from the team's intelligence config.

    Returns an empty string when the mapping cannot be parsed or the default
    model does not match a known provider. Shared with the scenario diagnostics
    service so missing-key short-circuits stay consistent with the static
    verify checks.
    """
    try:
        mapping = cast(
            dict[str, Any],
            load_yaml_file(get_config_path("intelligences/model_mapping.yml")),
        )
        default_model = str(mapping.get("default", ""))
        if "openai" in default_model:
            return "openai"
        if "gemini" in default_model or "google" in default_model:
            return "gemini"
        if "anthropic" in default_model or "claude" in default_model:
            return "anthropic"
    except Exception:
        pass

    return ""


class VerifyService:
    def verify(
        self,
        *,
        config: ConfigStatus,
        team: Team | None,
        team_error: Exception | None = None,
    ) -> VerifyResponse:
        env = self._read_env(config.env_file)
        checks = [
            *self._check_files(config),
            *self._check_team(team, team_error),
        ]
        if team is not None:
            checks.extend(self._check_llm_provider(team, env))
            checks.extend(self._check_cli_agent(env))
            checks.extend(self._check_github_credentials(team, env))

        errors = [check for check in checks if check.status == "error"]
        warnings = [check for check in checks if check.status == "warning"]
        active_members = (
            [member.person_id for member in team.members if member.is_active]
            if team is not None
            else []
        )
        return VerifyResponse(
            ok=not errors,
            config=config,
            active_members=active_members,
            checks=checks,
            warnings=warnings,
            errors=errors,
        )

    def _check_files(self, config: ConfigStatus) -> list[VerifyCheck]:
        checks = [
            self._check(
                "config_project_file",
                config.project_file_exists,
                "Project config file was found.",
                "project.yml was not found in the config directory.",
                target=str(config.project_file),
            )
        ]
        if config.env_file_exists:
            checks.append(
                VerifyCheck(
                    code="env_file",
                    status="ok",
                    message=".env file was found.",
                    target=str(config.env_file),
                )
            )
        else:
            checks.append(
                VerifyCheck(
                    code="env_file",
                    status="warning",
                    message=".env file was not found.",
                    target=str(config.env_file),
                )
            )
        return checks

    def _check_team(
        self, team: Team | None, team_error: Exception | None
    ) -> list[VerifyCheck]:
        if team_error is not None:
            return [
                VerifyCheck(
                    code="team_load",
                    status="error",
                    message=str(team_error),
                    context={"error_type": type(team_error).__name__},
                )
            ]
        if team is None:
            return [
                VerifyCheck(
                    code="team_load",
                    status="error",
                    message="Team config could not be loaded.",
                )
            ]

        active_members = [
            member.person_id for member in team.members if member.is_active
        ]
        return [
            VerifyCheck(
                code="team_load",
                status="ok",
                message="Team config was loaded.",
            ),
            self._check(
                "active_members",
                bool(active_members),
                "Active members are configured.",
                "No active members are configured.",
                context={"active_members": active_members},
            ),
        ]

    def _check_llm_provider(
        self, team: Team, env: dict[str, str | None]
    ) -> list[VerifyCheck]:
        provider = self._resolve_default_model_provider(team)
        key = PROVIDER_ENV_KEYS.get(provider)

        if key is None:
            return [
                VerifyCheck(
                    code="llm_provider",
                    status="warning",
                    message="Default LLM provider could not be inferred.",
                    context={"provider": provider},
                )
            ]

        return [
            self._check(
                "llm_api_key",
                self._has_env(key, env),
                f"{key} is configured.",
                f"{key} is not configured.",
                target=key,
                context={"provider": provider},
            )
        ]

    def _check_cli_agent(self, env: dict[str, str | None]) -> list[VerifyCheck]:
        executable = self._resolve_default_cli_executable()
        if not executable:
            return [
                VerifyCheck(
                    code="cli_agent_mapping",
                    status="warning",
                    message="Default CLI agent executable could not be inferred.",
                )
            ]

        search_path = env.get("PATH")
        path = resolve_cli_agent_path(executable, search_path)
        return [
            self._check(
                "cli_agent_executable",
                bool(path),
                f"CLI agent executable '{executable}' was found.",
                f"CLI agent executable '{executable}' was not found on PATH.",
                target=executable,
                context={"path": path or ""},
            )
        ]

    def _check_github_credentials(
        self, team: Team, env: dict[str, str | None]
    ) -> list[VerifyCheck]:
        if not team.project.is_available_service(Service.TICKET_MANAGER):
            return []

        checks: list[VerifyCheck] = []
        for member in team.members:
            if not member.is_active:
                continue

            keys = self._github_required_keys(member)
            for key in keys:
                env_key = member.to_person_env_key(key)
                checks.append(
                    self._check(
                        "github_credential",
                        self._has_env(env_key, env),
                        f"{env_key} is configured.",
                        f"{env_key} is not configured.",
                        target=env_key,
                        context={"person_id": member.person_id, "key": key},
                    )
                )
        return checks

    def _github_required_keys(self, member: Person) -> list[str]:
        if member.person_type == GitHubAppAuth.GITHUB_APPS:
            return [
                "github_installation_id",
                "github_app_id",
                "github_private_key_path",
            ]
        if member.person_type in {
            GitHubAppAuth.MACHINE_USER,
            GitHubAppAuth.PROXY_AGENT,
        }:
            return ["github_access_token"]
        return []

    def _resolve_default_model_provider(self, team: Team) -> str:
        # The team parameter is retained for API stability; provider resolution
        # only depends on the workspace intelligence mapping.
        del team
        return resolve_default_model_provider()

    def _resolve_default_cli_executable(self) -> str:
        return resolve_default_cli_executable()

    def _read_env(self, env_file: Path) -> dict[str, str | None]:
        values: dict[str, str | None] = (
            dict(dotenv_values(env_file)) if env_file.exists() else {}
        )
        for key, value in os.environ.items():
            values.setdefault(key, value)
        return values

    def _has_env(self, key: str, env: dict[str, str | None]) -> bool:
        return bool(env.get(key))

    def _check(
        self,
        code: str,
        ok: bool,
        ok_message: str,
        error_message: str,
        *,
        target: str = "",
        context: dict[str, Any] | None = None,
    ) -> VerifyCheck:
        return VerifyCheck(
            code=code,
            status="ok" if ok else "error",
            message=ok_message if ok else error_message,
            target=target,
            context=context or {},
        )
