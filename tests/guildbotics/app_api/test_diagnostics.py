from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

from guildbotics.app_api import diagnostics as diagnostics_module
from guildbotics.app_api.diagnostics import ScenarioDiagnosticsService
from guildbotics.entities.team import (
    MessageChannel,
    Person,
    Project,
    Team,
)
from guildbotics.intelligences.brains.cli_agent import CliAgentBrain

TICKET_STATUSES = ["Todo", "Doing", "Done"]
CLI_AGENT_FAILURE_RETURNCODE = 2


@dataclass
class _CliResult:
    returncode: int = 0
    stdout: str = "OK"
    stderr: str = ""


class _StubBrain(CliAgentBrain):
    """Minimal CliAgentBrain stand-in returning a canned execution result."""

    def __init__(self, result: _CliResult) -> None:
        self._result = result

    async def run_with_execution_details(self, message: str, cwd: Any) -> _CliResult:
        return self._result


class _StubChatService:
    def __init__(self, *, bot_user_id: str = "U123") -> None:
        self.bot_user_id = bot_user_id

    async def get_bot_identity(self) -> Any:
        return type("Identity", (), {"user_id": self.bot_user_id})()

    async def resolve_channel_id(self, channel_name: str) -> str:
        return f"C-{channel_name}"

    async def list_channel_events(self, channel_id: str, limit: int = 1) -> list:
        return []


class _StubTicketManager:
    def __init__(self) -> None:
        self.lane_map: dict[str, str | None] = {
            "ready": "Todo",
            "working": "Doing",
            "done": "Done",
        }
        self.assignable: bool = True
        self.agent_field_options: list[str] = []

    async def get_statuses(self) -> list[str]:
        return list(TICKET_STATUSES)

    async def is_assignable_user(self, username: str) -> bool:
        return self.assignable

    async def get_agent_field_options(self) -> list[str]:
        return list(self.agent_field_options)


@dataclass
class _StubContext:
    """Configurable Context stub shared across diagnostics test cases."""

    team: Team
    person: Person | None = None
    brain: Any = None
    ticket_manager: Any = field(default_factory=_StubTicketManager)
    chat_service: Any = field(default_factory=_StubChatService)
    talk_result: str = "OK"
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("diagnostics-test")
    )

    def clone_for(self, member: Person) -> _StubContext:
        clone = _StubContext(
            team=self.team,
            person=member,
            brain=self.brain,
            ticket_manager=self.ticket_manager,
            chat_service=self.chat_service,
            talk_result=self.talk_result,
            logger=self.logger,
        )
        return clone

    async def aclose(self) -> None:
        return None

    def get_brain(self, name: str, config: dict, _extra: Any) -> Any:
        return self.brain

    def get_ticket_manager(self) -> Any:
        return self.ticket_manager

    def get_chat_service(self) -> Any:
        return self.chat_service


def _person(person_id: str, **kwargs: Any) -> Person:
    return Person(person_id=person_id, name=person_id.title(), **kwargs)


def _team(members: list[Person], services: dict | None = None) -> Team:
    return Team(project=Project(name="demo", services=services or {}), members=members)


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable: str = "codex",
    path: str = "/usr/local/bin/codex",
) -> None:
    monkeypatch.setattr(
        diagnostics_module, "resolve_default_cli_executable", lambda: executable
    )
    monkeypatch.setattr(
        diagnostics_module, "resolve_cli_agent_path", lambda *_a, **_k: path
    )


def _patch_talk(monkeypatch: pytest.MonkeyPatch, result: Any = "OK") -> None:
    async def _talk(context: Any, *_args: Any, **_kwargs: Any) -> str:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(diagnostics_module, "talk_as", _talk)


def _patch_provider(monkeypatch: pytest.MonkeyPatch, provider: str = "openai") -> None:
    """Pin the resolved LLM provider so the missing-key gate is exercised.

    The provider resolver normally reads ``intelligences/model_mapping.yml``
    from the configured workspace; tests run in a tmp tree where that file is
    not seeded, so we set the value explicitly to make behavior deterministic.
    """
    monkeypatch.setattr(
        diagnostics_module, "resolve_default_model_provider", lambda: provider
    )


def _patch_app_token_probe(
    monkeypatch: pytest.MonkeyPatch, *, error: Exception | None = None
) -> None:
    async def _probe(app_token: str, base_url: Any) -> None:
        if error is not None:
            raise error

    monkeypatch.setattr(diagnostics_module, "probe_slack_app_token", _probe)


def _by_code(response: Any) -> dict:
    return {check.code: check for check in response.checks}


async def _run(
    context: Any,
    *,
    context_error: Exception | None = None,
    person_id: str | None = None,
) -> Any:
    return await ScenarioDiagnosticsService().run(
        context=context, context_error=context_error, person_id=person_id
    )


@pytest.mark.asyncio
async def test_context_construction_failure() -> None:
    response = await _run(None, context_error=RuntimeError("cannot build context"))

    checks = _by_code(response)
    assert checks["config_load"].status == "error"
    assert "cannot build context" in checks["config_load"].message
    assert not response.ok
    assert response.active_members == []


@pytest.mark.asyncio
async def test_context_missing_without_error() -> None:
    response = await _run(None)

    checks = _by_code(response)
    assert checks["config_load"].status == "error"
    assert checks["config_load"].message == "Configuration could not be loaded."


@pytest.mark.asyncio
async def test_person_id_not_found() -> None:
    context = _StubContext(team=_team([_person("alice", is_active=True)]))

    response = await _run(context, person_id="ghost")

    checks = _by_code(response)
    assert checks["member_not_found"].status == "error"
    assert checks["member_not_found"].person_id == "ghost"
    assert not response.ok


@pytest.mark.asyncio
async def test_no_active_members() -> None:
    context = _StubContext(team=_team([_person("alice", is_active=False)]))

    response = await _run(context)

    checks = _by_code(response)
    assert checks["active_members"].status == "error"
    assert checks["active_members"].message == "No active members are configured."


@pytest.mark.asyncio
async def test_person_id_inactive_member_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)
    _patch_talk(monkeypatch)
    context = _StubContext(team=_team([_person("alice", is_active=False)]))

    response = await _run(context, person_id="alice")

    codes = {check.code for check in response.checks}
    warning = next(c for c in response.warnings if c.code == "member_inactive")
    assert warning.person_id == "alice"
    assert warning.context["inactive_members"] == ["alice"]
    assert "active_members" in codes


@pytest.mark.asyncio
async def test_multiple_active_members_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli(monkeypatch)
    _patch_talk(monkeypatch)
    context = _StubContext(
        team=_team(
            [
                _person("alice", is_active=True),
                _person("bob", is_active=True),
                _person("carol", is_active=False),
            ]
        ),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    members_check = next(
        c for c in response.checks if c.code == "active_members" and c.status == "ok"
    )
    assert members_check.context["members"] == ["alice", "bob"]
    assert response.active_members == ["alice", "bob"]


@pytest.mark.asyncio
async def test_llm_live_call_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch)
    _patch_provider(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_talk(monkeypatch, "OK")
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["llm_live_call"]
    assert check.status == "ok"
    assert check.person_id == "alice"


@pytest.mark.asyncio
async def test_llm_live_call_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch)
    _patch_provider(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_talk(monkeypatch, RuntimeError("invalid api key"))
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["llm_live_call"]
    assert check.status == "error"
    assert "invalid api key" in check.message
    assert check.context["error_type"] == "RuntimeError"
    assert not response.ok


@pytest.mark.asyncio
async def test_llm_check_skipped_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing provider key short-circuits the LLM check WITHOUT firing talk_as.

    This keeps the scenario diagnostics offline-deterministic so the desktop
    E2E does not depend on a live OpenAI round-trip (and so CI runners and
    offline dev environments do not produce false negatives).
    """
    _patch_cli(monkeypatch)
    _patch_provider(monkeypatch, "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    talk_calls: list[Any] = []

    async def _talk(*args: Any, **kwargs: Any) -> str:  # pragma: no cover
        talk_calls.append((args, kwargs))
        return "OK"

    monkeypatch.setattr(diagnostics_module, "talk_as", _talk)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)
    checks = _by_code(response)

    assert "llm_live_call" not in checks
    fast_error = checks["llm_api_key"]
    assert fast_error.status == "error"
    assert "OPENAI_API_KEY is not configured" in fast_error.message
    assert fast_error.context["provider"] == "openai"
    assert fast_error.context["env_key"] == "OPENAI_API_KEY"
    assert fast_error.person_id == "alice"
    assert talk_calls == []


@pytest.mark.asyncio
async def test_llm_check_falls_through_when_provider_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unmappable provider does not short-circuit; the live path still runs.

    Preserves backwards-compatible behavior for configurations whose default
    model name does not match a known provider.
    """
    _patch_cli(monkeypatch)
    _patch_provider(monkeypatch, "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_talk(monkeypatch, "OK")
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["llm_live_call"]
    assert check.status == "ok"


@pytest.mark.asyncio
async def test_cli_agent_mapping_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_talk(monkeypatch)
    monkeypatch.setattr(
        diagnostics_module, "resolve_default_cli_executable", lambda: ""
    )
    context = _StubContext(team=_team([_person("alice", is_active=True)]))

    response = await _run(context)

    check = _by_code(response)["cli_agent_mapping"]
    assert check.status == "error"
    assert check.message == "Default CLI agent executable could not be inferred."


@pytest.mark.asyncio
async def test_cli_agent_executable_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch, executable="codex", path="")
    context = _StubContext(team=_team([_person("alice", is_active=True)]))

    response = await _run(context)

    check = _by_code(response)["cli_agent_executable"]
    assert check.status == "error"
    assert check.target == "codex"
    assert "not found on PATH" in check.message


@pytest.mark.asyncio
async def test_cli_agent_executable_found_and_brain_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch, path="/usr/local/bin/codex")
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult(returncode=0, stdout="OK")),
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["cli_agent_executable"].status == "ok"
    assert checks["cli_agent_executable"].context["path"] == "/usr/local/bin/codex"
    assert checks["cli_agent_brain"].status == "ok"
    assert checks["cli_agent_brain"].person_id == "alice"


@pytest.mark.asyncio
async def test_cli_agent_brain_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(
            _CliResult(
                returncode=CLI_AGENT_FAILURE_RETURNCODE,
                stdout="",
                stderr="login required",
            )
        ),
    )

    response = await _run(context)

    check = _by_code(response)["cli_agent_brain"]
    assert check.status == "error"
    assert check.context["returncode"] == CLI_AGENT_FAILURE_RETURNCODE
    assert "login required" in check.message


@pytest.mark.asyncio
async def test_cli_agent_brain_empty_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult(returncode=0, stdout="   ", stderr="")),
    )

    response = await _run(context)

    check = _by_code(response)["cli_agent_brain"]
    assert check.status == "error"
    assert check.context["empty_stdout"] is True


@pytest.mark.asyncio
async def test_cli_agent_brain_wrong_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=object(),
    )

    response = await _run(context)

    check = _by_code(response)["cli_agent_brain"]
    assert check.status == "error"
    assert "not CliAgentBrain" in check.message


@pytest.mark.asyncio
async def test_github_not_configured_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["github_not_configured"]
    assert check.status == "ok"
    assert "skipped" in check.message


@pytest.mark.asyncio
async def test_github_enabled_project_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    services = {
        "ticket_manager": {"name": "GitHub"},
        "code_hosting_service": {"name": "GitHub"},
    }
    context = _StubContext(
        team=_team(
            [
                _person(
                    "alice",
                    is_active=True,
                    account_info={"github_username": "alice"},
                )
            ],
            services=services,
        ),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["github_project_access"].status == "ok"
    assert checks["github_lane_mapping"].status == "ok"
    assert checks["github_agent_assignment"].status == "ok"
    assert checks["github_project_access"].context["status_count"] == len(
        TICKET_STATUSES
    )
    # The repository is derived from each issue at runtime, so there is no
    # repository pre-flight check (and no default repository to validate).
    assert "github_repository_access" not in checks


def _github_services() -> dict:
    return {
        "ticket_manager": {"name": "GitHub"},
        "code_hosting_service": {"name": "GitHub"},
    }


@pytest.mark.asyncio
async def test_github_missing_ready_lane_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    ticket_manager = _StubTicketManager()
    # "Backlog" is not among TICKET_STATUSES, so the ready lane is missing.
    ticket_manager.lane_map = {"ready": "Backlog", "working": "Doing", "done": "Done"}
    context = _StubContext(
        team=_team(
            [
                _person(
                    "alice", is_active=True, account_info={"github_username": "alice"}
                )
            ],
            services=_github_services(),
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=ticket_manager,
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["github_lane_missing"].status == "error"
    assert "Backlog" in checks["github_lane_missing"].message
    assert not response.ok


@pytest.mark.asyncio
async def test_github_missing_working_lane_is_warning_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    ticket_manager = _StubTicketManager()
    ticket_manager.lane_map = {
        "ready": "Todo",
        "working": "Nowhere",
        "done": "Done",
    }
    context = _StubContext(
        team=_team(
            [
                _person(
                    "alice", is_active=True, account_info={"github_username": "alice"}
                )
            ],
            services=_github_services(),
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=ticket_manager,
    )

    response = await _run(context)

    checks = _by_code(response)
    assert "github_lane_missing" not in checks
    assert checks["github_working_lane_missing"].status == "warning"
    assert all(check.code != "github_working_lane_missing" for check in response.errors)


@pytest.mark.asyncio
async def test_github_non_assignable_human_advises_repo_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    ticket_manager = _StubTicketManager()
    # A human member with a GitHub username who is not a repo collaborator: the
    # Agent field does not apply, so the remediation is repo permissions.
    ticket_manager.assignable = False
    context = _StubContext(
        team=_team(
            [
                _person(
                    "alice", is_active=True, account_info={"github_username": "alice"}
                )
            ],
            services=_github_services(),
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=ticket_manager,
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["github_member_not_assignable"].status == "error"
    assert "github_agent_field_required" not in checks
    assert not response.ok


@pytest.mark.asyncio
async def test_github_proxy_agent_without_agent_field_option_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    ticket_manager = _StubTicketManager()
    ticket_manager.assignable = False
    ticket_manager.agent_field_options = []
    context = _StubContext(
        team=_team(
            [_person("bot", is_active=True, person_type="proxy_agent")],
            services=_github_services(),
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=ticket_manager,
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["github_agent_field_required"].status == "error"
    assert checks["github_agent_field_required"].context["agent_option"] == "⚙bot"
    assert not response.ok


@pytest.mark.asyncio
async def test_github_proxy_agent_with_agent_field_option_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    ticket_manager = _StubTicketManager()
    ticket_manager.assignable = False
    ticket_manager.agent_field_options = ["⚙bot"]
    context = _StubContext(
        team=_team(
            [_person("bot", is_active=True, person_type="proxy_agent")],
            services=_github_services(),
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=ticket_manager,
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["github_agent_assignment"].status == "ok"
    assert "github_agent_field_required" not in checks


@pytest.mark.asyncio
async def test_github_access_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)

    class _FailingTicketManager:
        async def get_statuses(self) -> list[str]:
            raise RuntimeError("403 forbidden")

    context = _StubContext(
        team=_team(
            [_person("alice", is_active=True)],
            services={"ticket_manager": {"name": "GitHub"}},
        ),
        brain=_StubBrain(_CliResult()),
        ticket_manager=_FailingTicketManager(),
    )

    response = await _run(context)

    check = _by_code(response)["github_access"]
    assert check.status == "error"
    assert "403 forbidden" in check.message
    assert not response.ok


def _slack_member(person_id: str, *, enabled: bool = True) -> Person:
    channel = MessageChannel(
        name="general",
        service="slack",
        chat={"enabled": enabled, "channel_id": "C001", "channel_name": "general"},
    )
    return _person(person_id, is_active=True, message_channels=[channel])


@pytest.mark.asyncio
async def test_slack_not_configured_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    context = _StubContext(
        team=_team([_person("alice", is_active=True)]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["slack_not_configured"]
    assert check.status == "ok"


@pytest.mark.asyncio
async def test_slack_credential_present_and_channel_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    _patch_app_token_probe(monkeypatch)
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-token")
    context = _StubContext(
        team=_team([_slack_member("alice")]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    checks = _by_code(response)
    assert checks["slack_app_token"].status == "ok"
    assert checks["slack_bot_auth"].status == "ok"
    assert checks["slack_bot_auth"].context["bot_user_id"] == "U123"
    assert checks["slack_channel_history"].status == "ok"


@pytest.mark.asyncio
async def test_slack_app_token_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    from guildbotics.capabilities.member_github import MemberCapabilityError

    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    _patch_app_token_probe(monkeypatch, error=MemberCapabilityError("invalid_auth"))
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-broken")
    context = _StubContext(
        team=_team([_slack_member("alice")]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["slack_app_token_invalid"]
    assert check.status == "error"
    assert check.target == "ALICE_SLACK_APP_TOKEN"
    assert "invalid_auth" in check.message
    assert not response.ok


@pytest.mark.asyncio
async def test_slack_credential_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    monkeypatch.delenv("ALICE_SLACK_APP_TOKEN", raising=False)
    context = _StubContext(
        team=_team([_slack_member("alice")]),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context)

    check = _by_code(response)["slack_app_token"]
    assert check.status == "error"
    assert check.target == "ALICE_SLACK_APP_TOKEN"
    assert not response.ok


@pytest.mark.asyncio
async def test_slack_access_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    _patch_app_token_probe(monkeypatch)
    monkeypatch.setenv("ALICE_SLACK_APP_TOKEN", "xapp-token")

    class _FailingChat(_StubChatService):
        async def get_bot_identity(self) -> Any:
            raise RuntimeError("invalid_auth")

    context = _StubContext(
        team=_team([_slack_member("alice")]),
        brain=_StubBrain(_CliResult()),
        chat_service=_FailingChat(),
    )

    response = await _run(context)

    check = _by_code(response)["slack_access"]
    assert check.status == "error"
    assert "invalid_auth" in check.message


@pytest.mark.asyncio
async def test_person_id_targets_single_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_talk(monkeypatch)
    _patch_cli(monkeypatch)
    _patch_provider(monkeypatch, "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    context = _StubContext(
        team=_team(
            [
                _person("alice", is_active=True),
                _person("bob", is_active=True),
            ]
        ),
        brain=_StubBrain(_CliResult()),
    )

    response = await _run(context, person_id="bob")

    assert response.active_members == ["bob"]
    llm_checks = [c for c in response.checks if c.code == "llm_live_call"]
    assert [c.person_id for c in llm_checks] == ["bob"]
