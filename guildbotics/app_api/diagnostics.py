from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

from guildbotics.app_api.cli_agents import (
    resolve_cli_agent_path,
    resolve_default_cli_executable,
)
from guildbotics.app_api.models import DiagnosticCheck, ScenarioDiagnosticsResponse
from guildbotics.app_api.verify import (
    PROVIDER_ENV_KEYS,
    resolve_default_model_provider,
)
from guildbotics.entities.message import Message
from guildbotics.entities.team import Person, Service
from guildbotics.integrations.chat_profile import get_chat_subscriptions
from guildbotics.integrations.github.github_ticket_manager import GitHubTicketManager
from guildbotics.intelligences.brains.cli_agent import CliAgentBrain
from guildbotics.intelligences.functions import talk_as
from guildbotics.runtime import Context

DiagnosticSection = Literal[
    "config", "members", "llm", "cli_agent", "github", "slack", "git"
]
DiagnosticStatus = Literal["ok", "warning", "error"]


class ScenarioDiagnosticsService:
    async def run(
        self,
        *,
        context: Context | None,
        context_error: Exception | None = None,
        person_id: str | None = None,
    ) -> ScenarioDiagnosticsResponse:
        checks: list[DiagnosticCheck] = []
        if context_error is not None:
            checks.append(
                self._check(
                    "config",
                    "config_load",
                    "error",
                    self._safe_error(
                        "Configuration could not be loaded", context_error
                    ),
                )
            )
            return self._response(checks, [])

        if context is None:
            checks.append(
                self._check(
                    "config",
                    "config_load",
                    "error",
                    "Configuration could not be loaded.",
                )
            )
            return self._response(checks, [])

        members = self._target_members(context, person_id)
        checks.append(
            self._check(
                "config",
                "config_load",
                "ok",
                "Configuration was loaded.",
            )
        )
        if person_id is not None and not members:
            checks.append(
                self._check(
                    "members",
                    "member_not_found",
                    "error",
                    "Member was not found.",
                    person_id=person_id,
                )
            )
            return self._response(checks, [])

        if not members:
            checks.append(
                self._check(
                    "members",
                    "active_members",
                    "error",
                    "No active members are configured.",
                )
            )
            return self._response(checks, [])

        inactive_members = [
            member.person_id for member in members if not member.is_active
        ]
        if inactive_members:
            checks.append(
                self._check(
                    "members",
                    "member_inactive",
                    "warning",
                    "Member is inactive; scheduler runtime will not use this member.",
                    person_id=inactive_members[0] if len(inactive_members) == 1 else "",
                    context={"inactive_members": inactive_members},
                )
            )
        checks.append(
            self._check(
                "members",
                "active_members",
                "ok",
                "Members are available for diagnostics.",
                context={"members": [member.person_id for member in members]},
            )
        )
        checks.extend(await self._check_llm(context, members[0]))
        checks.extend(await self._check_cli_agent(context, members))
        checks.extend(await self._check_github(context, members))
        checks.extend(await self._check_slack(context, members))
        return self._response(checks, [member.person_id for member in members])

    def _target_members(self, context: Context, person_id: str | None) -> list[Person]:
        if person_id:
            return [
                member
                for member in context.team.members
                if member.person_id == person_id
            ]
        return [member for member in context.team.members if member.is_active]

    async def _check_llm(
        self, context: Context, member: Person
    ) -> list[DiagnosticCheck]:
        # Short-circuit BEFORE firing a live LLM call when the provider's API
        # key is not configured. Without this gate the diagnostics journey
        # depends on external network availability and provider latency, which
        # conflicts with the repo's "no external service calls in tests"
        # policy. The static verify check has long behaved this way; the
        # scenario diagnostics now match.
        provider = resolve_default_model_provider()
        env_key = PROVIDER_ENV_KEYS.get(provider)
        if env_key is not None and not os.environ.get(env_key):
            return [
                self._check(
                    "llm",
                    "llm_api_key",
                    "error",
                    f"{env_key} is not configured; skipping live LLM call.",
                    person_id=member.person_id,
                    context={"provider": provider, "env_key": env_key},
                )
            ]
        c = context.clone_for(member)
        try:
            messages = [
                Message(
                    content="Reply with exactly OK.",
                    author="User",
                    author_type=Message.USER,
                    timestamp="",
                )
            ]
            await talk_as(c, "Reply with exactly OK.", "diagnostics", messages)
            return [
                self._check(
                    "llm",
                    "llm_live_call",
                    "ok",
                    "LLM provider accepted a minimal request.",
                    person_id=member.person_id,
                )
            ]
        except Exception as exc:
            return [
                self._check(
                    "llm",
                    "llm_live_call",
                    "error",
                    self._safe_error("LLM live check failed", exc),
                    person_id=member.person_id,
                    context={"error_type": type(exc).__name__},
                )
            ]
        finally:
            await c.aclose()

    async def _check_cli_agent(
        self, context: Context, members: list[Person]
    ) -> list[DiagnosticCheck]:
        executable = self._resolve_default_cli_executable()
        if not executable:
            return [
                self._check(
                    "cli_agent",
                    "cli_agent_mapping",
                    "error",
                    "Default CLI agent executable could not be inferred.",
                )
            ]

        path = resolve_cli_agent_path(executable)
        if not path:
            return [
                self._check(
                    "cli_agent",
                    "cli_agent_executable",
                    "error",
                    f"CLI agent executable '{executable}' was not found on PATH.",
                    target=executable,
                )
            ]
        checks = [
            self._check(
                "cli_agent",
                "cli_agent_executable",
                "ok",
                f"CLI agent executable '{executable}' was found.",
                target=executable,
                context={"path": path},
            )
        ]
        for member in members:
            checks.append(
                await self._check_cli_agent_brain(context, member, executable)
            )
        return checks

    async def _check_cli_agent_brain(
        self, context: Context, member: Person, executable: str
    ) -> DiagnosticCheck:
        c = context.clone_for(member)
        try:
            config = {
                "brain": "cli",
                "body": (
                    "You are validating that the configured CLI agent can run. "
                    "Reply with exactly OK and perform no file changes."
                ),
                "template_engine": "default",
            }
            message = (
                "This is a read-only diagnostics check. "
                "Reply with exactly OK. Do not create, modify, delete, "
                "or inspect unrelated files."
            )
            brain = c.get_brain("diagnostics/cli_agent", config, None)
            if not isinstance(brain, CliAgentBrain):
                return self._check(
                    "cli_agent",
                    "cli_agent_brain",
                    "error",
                    "Configured CLI diagnostics brain is not CliAgentBrain.",
                    person_id=member.person_id,
                    target=executable,
                    context={"brain_type": type(brain).__name__},
                )
            with tempfile.TemporaryDirectory(
                prefix="guildbotics-diagnostics-cli-"
            ) as tmp:
                result = await brain.run_with_execution_details(message, cwd=Path(tmp))

            if result.returncode != 0:
                return self._check(
                    "cli_agent",
                    "cli_agent_brain",
                    "error",
                    self._format_cli_agent_error(
                        "CLI agent command failed",
                        result.stderr,
                        result.stdout,
                        result.returncode,
                    ),
                    person_id=member.person_id,
                    target=executable,
                    context={
                        "executable": executable,
                        "returncode": result.returncode,
                        "stderr": self._truncate(result.stderr),
                        "stdout": self._truncate(result.stdout),
                    },
                )

            if not result.stdout.strip():
                c.logger.warning(
                    "CLI agent diagnostics produced empty stdout for "
                    "person=%s executable=%s stderr=%s",
                    member.person_id,
                    executable,
                    self._truncate(result.stderr),
                )
                return self._check(
                    "cli_agent",
                    "cli_agent_brain",
                    "error",
                    self._format_cli_agent_error(
                        "CLI agent command completed but returned no response",
                        result.stderr,
                        result.stdout,
                        result.returncode,
                    ),
                    person_id=member.person_id,
                    target=executable,
                    context={
                        "executable": executable,
                        "empty_stdout": True,
                        "stderr": self._truncate(result.stderr),
                    },
                )
            return self._check(
                "cli_agent",
                "cli_agent_brain",
                "ok",
                "CLI agent accepted a minimal read-only request.",
                person_id=member.person_id,
            )
        except Exception as exc:
            c.logger.warning(
                "CLI agent diagnostics failed for person=%s executable=%s: %s",
                member.person_id,
                executable,
                exc,
            )
            return self._check(
                "cli_agent",
                "cli_agent_brain",
                "error",
                self._safe_error("CLI agent brain check failed", exc),
                person_id=member.person_id,
                target=executable,
                context={"error_type": type(exc).__name__, "executable": executable},
            )
        finally:
            await c.aclose()

    async def _check_github(
        self, context: Context, members: list[Person]
    ) -> list[DiagnosticCheck]:
        project = context.team.project
        if not (
            project.is_available_service(Service.TICKET_MANAGER)
            or project.is_available_service(Service.CODE_HOSTING_SERVICE)
        ):
            return [
                self._check(
                    "github",
                    "github_not_configured",
                    "ok",
                    "GitHub integration is not configured; GitHub diagnostics were skipped.",
                )
            ]

        checks: list[DiagnosticCheck] = []
        for member in members:
            c = context.clone_for(member)
            try:
                if project.is_available_service(Service.TICKET_MANAGER):
                    ticket_manager = cast(GitHubTicketManager, c.get_ticket_manager())
                    statuses = await ticket_manager.get_statuses()
                    checks.append(
                        self._check(
                            "github",
                            "github_project_access",
                            "ok",
                            "GitHub project status options were fetched.",
                            person_id=member.person_id,
                            context={"status_count": len(statuses)},
                        )
                    )
                if project.is_available_service(Service.CODE_HOSTING_SERVICE):
                    default_branch = (
                        await c.get_code_hosting_service().get_default_branch()
                    )
                    checks.append(
                        self._check(
                            "git",
                            "github_repository_access",
                            "ok",
                            "GitHub repository metadata was fetched.",
                            person_id=member.person_id,
                            context={"default_branch": default_branch},
                        )
                    )
            except Exception as exc:
                checks.append(
                    self._check(
                        "github",
                        "github_access",
                        "error",
                        self._safe_error("GitHub read-only check failed", exc),
                        person_id=member.person_id,
                        context={"error_type": type(exc).__name__},
                    )
                )
            finally:
                await c.aclose()
        return checks

    async def _check_slack(
        self, context: Context, members: list[Person]
    ) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        has_slack = False
        for member in members:
            subscriptions = [
                sub
                for sub in get_chat_subscriptions(member)
                if bool(sub.get("enabled", True))
                and str(sub.get("service", "slack")).lower() == "slack"
            ]
            if not subscriptions:
                continue
            has_slack = True
            if not member.has_secret("SLACK_APP_TOKEN"):
                checks.append(
                    self._check(
                        "slack",
                        "slack_app_token",
                        "error",
                        "Slack App token is required for Socket Mode runtime.",
                        person_id=member.person_id,
                        target=member.to_person_env_key("SLACK_APP_TOKEN"),
                    )
                )
            c = context.clone_for(member)
            try:
                chat_service = c.get_chat_service()
                identity = await chat_service.get_bot_identity()
                checks.append(
                    self._check(
                        "slack",
                        "slack_bot_auth",
                        "ok",
                        "Slack bot authentication succeeded.",
                        person_id=member.person_id,
                        context={"bot_user_id": identity.user_id},
                    )
                )
                for sub in subscriptions:
                    checks.append(
                        await self._check_slack_channel(c, sub, member.person_id)
                    )
            except Exception as exc:
                checks.append(
                    self._check(
                        "slack",
                        "slack_access",
                        "error",
                        self._safe_error("Slack read-only check failed", exc),
                        person_id=member.person_id,
                        context={"error_type": type(exc).__name__},
                    )
                )
            finally:
                await c.aclose()

        if not has_slack:
            checks.append(
                self._check(
                    "slack",
                    "slack_not_configured",
                    "ok",
                    "Slack channels are not configured; Slack diagnostics were skipped.",
                )
            )
        return checks

    async def _check_slack_channel(
        self, context: Context, subscription: dict[str, Any], person_id: str
    ) -> DiagnosticCheck:
        chat_service = context.get_chat_service()
        channel_id = str(subscription.get("channel_id", "") or "").strip()
        channel_name = str(subscription.get("channel_name", "") or "").strip()
        target = channel_id or channel_name
        if not channel_id and channel_name:
            channel_id = await chat_service.resolve_channel_id(channel_name) or ""
        if not channel_id:
            return self._check(
                "slack",
                "slack_channel",
                "error",
                "Slack channel could not be resolved.",
                person_id=person_id,
                target=target,
            )
        await chat_service.list_channel_events(channel_id, limit=1)
        return self._check(
            "slack",
            "slack_channel_history",
            "ok",
            "Slack channel history was fetched.",
            person_id=person_id,
            target=target or channel_id,
        )

    def _resolve_default_cli_executable(self) -> str:
        return resolve_default_cli_executable()

    def _response(
        self, checks: list[DiagnosticCheck], active_members: list[str]
    ) -> ScenarioDiagnosticsResponse:
        errors = [check for check in checks if check.status == "error"]
        warnings = [check for check in checks if check.status == "warning"]
        return ScenarioDiagnosticsResponse(
            ok=not errors,
            active_members=active_members,
            checks=checks,
            warnings=warnings,
            errors=errors,
        )

    def _check(
        self,
        section: DiagnosticSection,
        code: str,
        status: DiagnosticStatus,
        message: str,
        *,
        target: str = "",
        person_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> DiagnosticCheck:
        return DiagnosticCheck(
            section=section,
            code=code,
            status=status,
            message=message,
            target=target,
            person_id=person_id,
            context=context or {},
        )

    def _safe_error(self, prefix: str, exc: Exception) -> str:
        message = str(exc).strip()
        if not message:
            message = type(exc).__name__
        return f"{prefix}: {message}"

    def _format_cli_agent_error(
        self, prefix: str, stderr: str, stdout: str, returncode: int
    ) -> str:
        details = [f"{prefix} (exit code: {returncode})."]
        if stderr.strip():
            details.append(f"stderr: {self._truncate(stderr)}")
        elif stdout.strip():
            details.append(f"stdout: {self._truncate(stdout)}")
        else:
            details.append(
                "No stderr was returned. Check that the CLI is logged in, can run "
                "in non-interactive mode, and prints its answer to stdout."
            )
        return " ".join(details)

    def _truncate(self, value: str, limit: int = 1000) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."
