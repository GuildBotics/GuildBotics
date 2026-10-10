from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

from guildbotics.app_api.models import DiagnosticCheck, ScenarioDiagnosticsResponse
from guildbotics.app_api.verify import (
    resolve_default_model_provider,
)
from guildbotics.commands.errors import CommandError
from guildbotics.drivers.command_runner import prepare_command, run_main_command
from guildbotics.entities.message import Message
from guildbotics.entities.team import Person
from guildbotics.integrations.factory import configured_providers
from guildbotics.integrations.provider import ProviderCheck
from guildbotics.intelligences.agent_runtime.models import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.intelligences.brains.factory import cli_agent_of, command_config
from guildbotics.intelligences.brains.inference import InferenceFailure
from guildbotics.intelligences.common import find_cli_agent_execution_error
from guildbotics.intelligences.functions import talk_as
from guildbotics.intelligences.llm_providers import provider_env_keys
from guildbotics.runtime import Context

DiagnosticSection = Literal[
    "config", "members", "llm", "cli_agent", "github", "slack", "git"
]
DiagnosticStatus = Literal["ok", "warning", "error"]


#: The bundled read-only command whose one AI CLI turn is the check: every turn
#: runs inside a command, whose environment holds it.
_CLI_AGENT_CHECK_COMMAND = "diagnostics/cli_agent"


async def _run_cli_agent_check(
    context: Context, member: Person, cwd: str
) -> CliAgentExecutionResult:
    """Run the check command and return what the AI CLI tool did.

    A tool that fails or answers nothing fails the command; its result still
    says how, which is what the check reports.
    """
    try:
        command = prepare_command(
            context, _CLI_AGENT_CHECK_COMMAND, [], member.person_id, Path(cwd)
        )
        try:
            outcome = await run_main_command(command, source="manual")
        finally:
            await command.context.aclose()
    except CommandError as exc:
        failure = find_cli_agent_execution_error(exc)
        if failure is None:
            raise
        return cast(CliAgentExecutionError, failure).result
    return CliAgentExecutionResult(stdout=outcome.text_output, stderr="", returncode=0)


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

        if person_id is not None and members[0].person_type == "human":
            checks.extend(await self._check_human_member(context, members[0]))
            return self._response(checks, [members[0].person_id])

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
        checks.extend(await self._check_providers(context, members))
        return self._response(checks, [member.person_id for member in members])

    async def _check_human_member(
        self, context: Context, member: Person
    ) -> list[DiagnosticCheck]:
        checks = [
            self._check(
                "members",
                "human_member_reference",
                "ok",
                "Human member config was loaded as a reference member, not an AI runtime subject.",
                person_id=member.person_id,
            )
        ]
        if member.roles:
            checks.append(
                self._check(
                    "members",
                    "human_member_roles",
                    "ok",
                    "Human member roles are configured.",
                    person_id=member.person_id,
                    context={"roles": sorted(member.roles)},
                )
            )
        else:
            checks.append(
                self._check(
                    "members",
                    "human_member_roles",
                    "error",
                    "Human member roles are not configured.",
                    person_id=member.person_id,
                )
            )

        for provider in configured_providers(context.team):
            checks.extend(
                _check(check) for check in await provider.diagnose(context, [member])
            )
        return checks

    def _target_members(self, context: Context, person_id: str | None) -> list[Person]:
        if person_id:
            return [
                member
                for member in context.team.members
                if member.person_id == person_id
            ]
        return [
            member
            for member in context.team.members
            if member.is_active and member.person_type != "human"
        ]

    async def _check_llm(
        self, context: Context, member: Person
    ) -> list[DiagnosticCheck]:
        # Short-circuit BEFORE firing a live LLM call when the provider's API
        # key is not configured. Without this gate the diagnostics journey
        # depends on external network availability and provider latency, which
        # conflicts with the repo's "no external service calls in tests"
        # policy. The static verify check has long behaved this way; the
        # scenario diagnostics now match.
        from guildbotics.utils.fileio import get_config_path

        provider = resolve_default_model_provider()
        env_key = provider_env_keys(get_config_path("")).get(provider)
        if env_key and not os.environ.get(env_key):
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
                    context={"provider": provider},
                )
            ]
        except Exception as exc:
            facts: dict[str, Any] = {
                "provider": provider,
                "error_type": type(exc).__name__,
            }
            if isinstance(exc, InferenceFailure):
                # What the provider said, not the wrapper the call raised.
                facts.update(error_type=exc.error_type, category=exc.category)
            return [
                self._check(
                    "llm",
                    "llm_live_call",
                    "error",
                    self._safe_error("LLM live check failed", exc),
                    person_id=member.person_id,
                    context=facts,
                )
            ]
        finally:
            await c.aclose()

    async def _check_cli_agent(
        self, context: Context, members: list[Person]
    ) -> list[DiagnosticCheck]:
        checks: list[DiagnosticCheck] = []
        for member in members:
            checks.extend(await self._check_cli_agent_brain(context, member))
        return checks

    async def _check_cli_agent_brain(
        self, context: Context, member: Person
    ) -> list[DiagnosticCheck]:
        """Run one read-only turn of the member's AI CLI tool.

        The tool runs inside the isolated agent environment, never on the
        host, so nothing is looked up on the host PATH: a device that cannot
        start the turn refuses it in the environment status's own words, and
        that refusal is what this check reports.
        """
        c = context.clone_for(member)
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        target = ""
        try:
            c.pipe = (
                "This is a read-only diagnostics check. "
                "Reply with exactly OK. Do not create, modify, delete, "
                "or inspect unrelated files."
            )
            brain = command_config(
                member.person_id,
                _CLI_AGENT_CHECK_COMMAND,
                c.team.project.get_language_code(),
            ).get("brain", "default")
            tool = cli_agent_of(member.person_id, brain)
            if tool is None:
                return [
                    self._check(
                        "cli_agent",
                        "cli_agent_brain",
                        "error",
                        "Configured CLI diagnostics brain runs no AI CLI tool.",
                        person_id=member.person_id,
                        context={"brain": brain},
                    )
                ]
            target = tool
            temporary_directory = tempfile.TemporaryDirectory(
                prefix="guildbotics-diagnostics-cli-"
            )
            result = await _run_cli_agent_check(c, member, temporary_directory.name)

            if result.returncode != 0:
                return [
                    self._check(
                        "cli_agent",
                        "cli_agent_brain",
                        "error",
                        self._format_cli_agent_error(
                            "AI CLI tool command failed",
                            result.stderr,
                            result.stdout,
                            result.returncode,
                        ),
                        person_id=member.person_id,
                        target=target,
                        context={
                            "returncode": result.returncode,
                            "stderr": self._truncate(result.stderr),
                            "stdout": self._truncate(result.stdout),
                        },
                    )
                ]

            if not result.stdout.strip():
                c.logger.warning(
                    "AI CLI tool diagnostics produced empty stdout for "
                    "person=%s tool=%s stderr=%s",
                    member.person_id,
                    target,
                    self._truncate(result.stderr),
                )
                return [
                    self._check(
                        "cli_agent",
                        "cli_agent_brain",
                        "error",
                        self._format_cli_agent_error(
                            "AI CLI tool command completed but returned no response",
                            result.stderr,
                            result.stdout,
                            result.returncode,
                        ),
                        person_id=member.person_id,
                        target=target,
                        context={
                            "empty_stdout": True,
                            "stderr": self._truncate(result.stderr),
                        },
                    )
                ]
            return [
                self._check(
                    "cli_agent",
                    "cli_agent_brain",
                    "ok",
                    "AI CLI tool accepted a minimal read-only request.",
                    person_id=member.person_id,
                    target=target,
                )
            ]
        except Exception as exc:
            c.logger.warning(
                "AI CLI tool diagnostics failed for person=%s tool=%s: %s",
                member.person_id,
                target,
                exc,
            )
            return [
                self._check(
                    "cli_agent",
                    "cli_agent_brain",
                    "error",
                    self._safe_error("AI CLI tool brain check failed", exc),
                    person_id=member.person_id,
                    target=target,
                    context={"error_type": type(exc).__name__},
                )
            ]
        finally:
            try:
                await c.aclose()
            finally:
                if temporary_directory is not None:
                    temporary_directory.cleanup()

    async def _check_providers(
        self, context: Context, members: list[Person]
    ) -> list[DiagnosticCheck]:
        providers = configured_providers(context.team)
        checks = [
            _check(check)
            for provider in providers
            for check in await provider.diagnose(context, members)
        ]
        if not any(p.code_hosting or p.ticket_manager for p in providers):
            checks.append(
                self._check(
                    "github",
                    "github_not_configured",
                    "ok",
                    "GitHub integration is not configured; GitHub diagnostics were skipped.",
                )
            )
        if not any(p.chat for p in providers):
            checks.append(
                self._check(
                    "slack",
                    "slack_not_configured",
                    "ok",
                    "Chat is not configured; chat diagnostics were skipped.",
                )
            )
        return checks

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


def _check(check: ProviderCheck) -> DiagnosticCheck:
    return DiagnosticCheck.model_validate(check.model_dump())
