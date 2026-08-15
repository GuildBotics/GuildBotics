from __future__ import annotations

import os
from typing import Any, cast

from guildbotics.app_api.models import ConfigStatus, VerifyCheck, VerifyResponse
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.github_utils import (
    GitHubAppAuth,
    get_github_account_type,
)
from guildbotics.intelligences.cli_agents import (
    resolve_cli_agent_path,
    resolve_default_cli_executable,
)
from guildbotics.intelligences.llm_providers import provider_env_keys
from guildbotics.utils.env_loader import workspace_secret_store
from guildbotics.utils.fileio import get_config_path, load_yaml_file


def resolve_default_model_provider() -> str:
    """Return the default LLM provider name from the team's intelligence config.

    The default model path is ``models/<provider>/<file>.yml``, so the provider
    is the second path segment. Returns an empty string when the mapping cannot
    be parsed. Shared with the scenario diagnostics service so missing-key
    short-circuits stay consistent with the static verify checks.
    """
    try:
        mapping = cast(
            dict[str, Any],
            load_yaml_file(get_config_path("intelligences/model_mapping.yml")),
        )
        parts = str(mapping.get("default", "")).split("/")
        if len(parts) > 1:
            return parts[1]
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
        env: dict[str, str | None] = dict(os.environ)
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
        from guildbotics.utils.secret_store import keyring_status

        store = keyring_status()
        if store["available"]:
            checks.append(
                VerifyCheck(
                    code="secret_store",
                    status="ok",
                    message="OS secret store is available.",
                )
            )
        else:
            checks.append(
                VerifyCheck(
                    code="secret_store",
                    status="error",
                    message="OS secret store is unavailable.",
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
        key = provider_env_keys(get_config_path("")).get(provider)

        if not key:
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
                self._has_env(key, env) or bool(workspace_secret_store().get(key)),
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
                    message="Default AI CLI tool executable could not be inferred.",
                )
            ]

        search_path = env.get("PATH")
        path = resolve_cli_agent_path(executable, search_path)
        return [
            self._check(
                "cli_agent_executable",
                bool(path),
                f"AI CLI tool executable '{executable}' was found.",
                f"AI CLI tool executable '{executable}' was not found on PATH.",
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
                if key in {"github_app_id", "github_installation_id"}:
                    configured = member.has_account_info(key)
                    target = f"account_info.{key}"
                elif key == "github_private_key":
                    configured = bool(
                        workspace_secret_store().get(
                            member.to_person_env_key("github_private_key")
                        )
                    )
                    target = member.to_person_env_key(key)
                else:
                    target = member.to_person_env_key(key)
                    configured = self._has_env(target, env) or bool(
                        workspace_secret_store().get(target)
                    )
                checks.append(
                    self._check(
                        "github_credential",
                        configured,
                        f"{target} is configured.",
                        f"{target} is not configured.",
                        target=target,
                        context={"person_id": member.person_id, "key": key},
                    )
                )
        return checks

    def _github_required_keys(self, member: Person) -> list[str]:
        github_account_type = get_github_account_type(member)
        if github_account_type == GitHubAppAuth.GITHUB_APPS:
            return [
                "github_installation_id",
                "github_app_id",
                "github_private_key",
            ]
        if github_account_type in {
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
