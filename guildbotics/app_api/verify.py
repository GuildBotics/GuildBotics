from __future__ import annotations

import os
from typing import Any, cast

from guildbotics.app_api.models import ConfigStatus, VerifyCheck, VerifyResponse
from guildbotics.entities.team import Team
from guildbotics.environment.status import (
    DeviceStatus,
    device_status,
)
from guildbotics.integrations.factory import configured_providers
from guildbotics.intelligences.cli_agents import resolve_default_cli_agent
from guildbotics.intelligences.llm_providers import provider_env_keys, provider_of
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
        return provider_of(str(mapping.get("default", "")))
    except Exception:
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
            device = device_status()
            checks.extend(self._check_llm_provider(team, env))
            checks.append(self._check_environment(device))
            checks.extend(self._check_cli_agent(device))
            checks.extend(self._check_credentials(team))

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

    def _check_environment(self, device: DeviceStatus) -> VerifyCheck:
        # Every command runs in the isolated agent environment, whether or
        # not it runs an AI CLI tool.
        return self._check(
            "agent_environment",
            not device.refusal,
            "The isolated agent environment can run commands on this device.",
            device.refusal,
        )

    def _check_cli_agent(self, device: DeviceStatus) -> list[VerifyCheck]:
        tool = resolve_default_cli_agent()
        if not tool:
            return [
                VerifyCheck(
                    code="cli_agent_mapping",
                    status="warning",
                    message="Default AI CLI tool could not be inferred.",
                )
            ]

        # The tool runs inside the isolated agent environment, so what decides
        # is whether it can start a turn there, not the host PATH; what the
        # device refuses is the environment check's to say.
        refusal = device.tool(tool).refusal
        return [
            self._check(
                "cli_agent_environment",
                not refusal,
                f"AI CLI tool '{tool}' can start in the isolated agent environment.",
                refusal,
                target=tool,
            )
        ]

    def _check_credentials(self, team: Team) -> list[VerifyCheck]:
        return [
            VerifyCheck(
                code=check.code,
                status=check.status,
                message=check.message,
                target=check.target,
                context=check.context,
            )
            for provider in configured_providers(team)
            for member in team.members
            if member.is_active
            for check in provider.verify(member)
        ]

    def _resolve_default_model_provider(self, team: Team) -> str:
        # The team parameter is retained for API stability; provider resolution
        # only depends on the workspace intelligence mapping.
        del team
        return resolve_default_model_provider()

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
