"""Direct unit tests for the command / scheduler methods of ``AppRuntime``.

Scope (session S4):

- ``AppRuntime.get_command_options``
- ``AppRuntime.run_command``
- ``AppRuntime.start_scheduler``

These complement the coarser API-level tests in ``test_api.py`` with
finer-grained assertions on file-resolution precedence, argument /
requirement extraction, published events, the run reservation lock and
routine rejection. Command execution and context creation are stubbed so
no real LLM / GitHub / subprocess I/O runs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest

from guildbotics.app_api import runtime as runtime_module
from guildbotics.app_api.command_files import encode_file_id, file_revision
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus, EventBusLogHandler
from guildbotics.app_api.models import (
    CommandAuthoringRequest,
    CommandRunRequest,
    SchedulerStartRequest,
    TroubleshootingFocus,
    TroubleshootingRequest,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.commands.authoring import (
    CommandAuthoringChange,
    CommandAuthoringResult,
)
from guildbotics.intelligences.brains.cli_agent import (
    CliAgentExecutionError,
    CliAgentExecutionResult,
)
from guildbotics.intelligences.troubleshooting import TroubleshootingResult
from guildbotics.commands.errors import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.drivers.execution import WorkRejectedError
from guildbotics.entities import Person, Project, Team
from guildbotics.observability import correlation_fields
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.runtime.service_lock import (
    ServiceLockMetadata,
    ServiceLockUnavailableError,
)

HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409


def _make_person(person_id: str = "bot", name: str = "Bot") -> Person:
    return Person(person_id=person_id, name=name, is_active=True)


def _make_context(
    members: list[Person],
    *,
    language_code: str = "en",
    github_enabled: bool = False,
    person: Person | None = None,
) -> object:
    # ``Context.get_default`` starts on a placeholder person until a member is
    # resolved; pass ``person`` to reproduce that pre-resolution state.
    person = person if person is not None else members[0]
    # A real team so member resolution (including the default executor) behaves
    # exactly as it does in production; only the context wrapper is a stub.
    services: dict = (
        {
            "ticket_manager": {"name": "GitHub", "owner": "acme"},
            "code_hosting_service": {"name": "GitHub", "owner": "acme"},
        }
        if github_enabled
        else {}
    )
    project = Project(name="demo", language=language_code, services=services)
    team = Team(project=project, members=members)

    def clone_for(self: object, selected: object) -> object:
        return _make_context(
            [selected],
            language_code=language_code,
            github_enabled=github_enabled,
        )

    async def aclose(self: object) -> None:
        self.closed = True

    return type(
        "ContextStub",
        (),
        {
            "team": team,
            "person": person,
            "clone_for": clone_for,
            "closed": False,
            "aclose": aclose,
        },
    )()


def _runtime_with_context(
    monkeypatch: pytest.MonkeyPatch, context: object
) -> AppRuntime:
    runtime = AppRuntime(EventBus())
    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)
    return runtime


def _isolate_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point cwd / HOME at ``tmp_path`` so command discovery is deterministic."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    return tmp_path / ".guildbotics/config"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# get_command_options
# ---------------------------------------------------------------------------


def test_command_options_prefers_workspace_over_home_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    home_dir = tmp_path / "home/.guildbotics/config"
    _write(
        home_dir / "commands/shared.md",
        "\n".join(["---", "name: Home Shared", "brain: none", "---", "Home body."]),
    )
    _write(
        config_dir / "commands/shared.md",
        "\n".join(
            ["---", "name: Workspace Shared", "brain: none", "---", "Workspace body."]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "shared"
    )

    assert option.label == "Workspace Shared"
    assert option.source == "workspace"


def test_command_options_prefers_member_specific_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Shared Greet", "brain: none", "---", "Shared."]),
    )
    _write(
        config_dir / "team/members/bot/commands/greet.md",
        "\n".join(["---", "name: Member Greet", "brain: none", "---", "Member."]),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "greet"
    )

    assert option.label == "Member Greet"


def test_command_options_localized_file_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/note.md",
        "\n".join(["---", "name: Base Note", "brain: none", "---", "Base."]),
    )
    _write(
        config_dir / "commands/note.en.md",
        "\n".join(["---", "name: English Note", "brain: none", "---", "English."]),
    )
    _write(
        config_dir / "commands/note.ja.md",
        "\n".join(["---", "name: Japanese Note", "brain: none", "---", "Japanese."]),
    )
    context = _make_context([_make_person()], language_code="ja")
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item for item in runtime.get_command_options().options if item.command == "note"
    )

    # `.ja` ranks above `.en`, which ranks above the base file.
    assert option.label == "Japanese Note"


def test_command_options_localized_falls_back_to_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/note.md",
        "\n".join(["---", "name: Base Note", "brain: none", "---", "Base."]),
    )
    _write(
        config_dir / "commands/note.en.md",
        "\n".join(["---", "name: English Note", "brain: none", "---", "English."]),
    )
    # Requested language is `de`; only `.en` and base exist -> `.en` wins.
    context = _make_context([_make_person()], language_code="de")
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item for item in runtime.get_command_options().options if item.command == "note"
    )

    assert option.label == "English Note"


def test_command_options_extract_python_signature_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/run_job.py",
        "\n".join(
            [
                "async def main(context, title, count='3', *, dry_run='False'):",
                '    """Run a job."""',
                "    return title",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "run_job"
    )

    assert option.description == "Run a job."
    assert option.inputs.model_dump() == {
        "defined_args": "auto",
        "extra_args": "hidden",
        "message": "optional",
    }
    assert [
        (arg.name, arg.kind, arg.required, arg.default) for arg in option.arguments
    ] == [
        ("title", "positional", True, ""),
        ("count", "positional", False, "3"),
        ("dry_run", "keyword", False, "False"),
    ]


def test_command_options_extract_yaml_frontmatter_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/yaml_task.yml",
        "\n".join(
            [
                "description: Convert ${1} using ${format}.",
                "commands:",
                "  - print: done",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "yaml_task"
    )

    assert [(arg.name, arg.kind) for arg in option.arguments] == [
        ("1", "positional"),
        ("format", "keyword"),
    ]


def test_command_options_apply_declared_argument_requiredness_and_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/summarize.md",
        "\n".join(
            [
                "---",
                "args:",
                "  file:",
                "    required: true",
                "  language:",
                "    default: English",
                "---",
                "Summarize ${file} using ${language} and ${style}.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "summarize"
    )

    assert [
        (arg.name, arg.kind, arg.required, arg.default) for arg in option.arguments
    ] == [
        ("file", "keyword", True, ""),
        ("language", "keyword", False, "English"),
        ("style", "keyword", True, ""),
    ]


def test_command_options_tolerate_invalid_argument_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/invalid.md",
        "\n".join(
            [
                "---",
                "args:",
                "  language:",
                "    required: true",
                "    default: English",
                "---",
                "Use ${language}.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "invalid"
    )

    assert option.arguments == []


def test_command_options_read_manual_input_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/manual.md",
        "\n".join(
            [
                "---",
                "inputs:",
                "  defined_args: hidden",
                "  extra_args: optional",
                "  message: required",
                "---",
                "Run ${internal_value}.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "manual"
    )

    assert option.inputs.model_dump() == {
        "defined_args": "hidden",
        "extra_args": "optional",
        "message": "required",
    }


def test_command_options_tolerate_invalid_manual_input_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/invalid.md",
        "\n".join(
            [
                "---",
                "inputs:",
                "  message: sometimes",
                "---",
                "Body.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "invalid"
    )

    assert option.inputs.model_dump() == {
        "defined_args": "auto",
        "extra_args": "hidden",
        "message": "optional",
    }


def test_command_options_detect_github_and_slack_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    _write(
        config_dir / "commands/integrations.py",
        "\n".join(
            [
                "from guildbotics.integrations.ticket_manager import TicketManager",
                "from guildbotics.integrations.chat_service import ChatService",
                "",
                "async def main(context):",
                "    return ''",
            ]
        ),
    )
    context = _make_context([_make_person()], github_enabled=True)
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_command_options().options
        if item.command == "integrations"
    )
    requirements = {req.kind: req for req in option.requirements}

    assert set(requirements) == {"github", "slack"}
    # github_enabled context -> github requirement is satisfied.
    assert requirements["github"].satisfied is True
    assert requirements["github"].message == "GitHub integration is required."
    # Slack tokens missing -> unsatisfied.
    assert requirements["slack"].satisfied is False


def test_command_options_detect_llm_and_cli_requirements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/llm_task.md",
        "\n".join(["---", "name: LLM Task", "---", "Summarize ${input}."]),
    )
    _write(
        config_dir / "commands/cli_task.md",
        "\n".join(["---", "name: Agent Task", "brain: agent", "---", "Edit ${file}."]),
    )
    _write(
        config_dir / "commands/static.md",
        "\n".join(["---", "name: Static", "brain: ' none '", "---", "Body."]),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    options = {item.command: item for item in runtime.get_command_options().options}

    assert {req.kind for req in options["llm_task"].requirements} == {"llm"}
    assert {req.kind for req in options["cli_task"].requirements} == {"cli_agent"}
    assert options["static"].requirements == []


def test_command_options_ignore_invalid_metadata_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    # Broken YAML and unparseable Python must not abort discovery.
    _write(config_dir / "commands/broken.yml", "::: not: valid: yaml:::\n- [")
    _write(config_dir / "commands/broken.py", "def main(:\n    pass")
    _write(
        config_dir / "commands/ok.md",
        "\n".join(["---", "name: Ok", "brain: none", "---", "Body."]),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    options = {item.command: item for item in runtime.get_command_options().options}

    assert "ok" in options
    # Invalid files are still listed but yield empty metadata / no requirements.
    assert options["broken"].requirements == []
    assert options["broken"].arguments == []


def test_command_options_person_not_found_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    context = _make_context([_make_person("bot", "Bot")])
    runtime = _runtime_with_context(monkeypatch, context)

    with pytest.raises(AppApiError) as exc_info:
        runtime.get_command_options(person="missing")

    assert exc_info.value.code == "person_not_found"
    assert exc_info.value.context["identifier"] == "missing"
    assert exc_info.value.context["available"] == ["bot"]


def test_command_options_member_scope_uses_cloned_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "team/members/alice/commands/private.md",
        "\n".join(["---", "name: Alice Only", "brain: none", "---", "Body."]),
    )
    members = [_make_person("bot", "Bot"), _make_person("alice", "Alice")]
    context = _make_context(members)
    runtime = _runtime_with_context(monkeypatch, context)

    # Without scoping to alice, bot's command roots do not include alice's dir.
    bot_commands = {
        item.command for item in runtime.get_command_options(person="bot").options
    }
    alice_commands = {
        item.command for item in runtime.get_command_options(person="alice").options
    }

    assert "private" not in bot_commands
    assert "private" in alice_commands


# ---------------------------------------------------------------------------
# get_routine_command_options
# ---------------------------------------------------------------------------


def test_routine_command_options_discover_only_declared_routines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only commands that self-declare ``routine: true`` are candidates. A plain
    # command is excluded, and the built-in template workflow (which declares
    # embedded routine metadata) is discovered through the same single pass.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/my_routine.md",
        "\n".join(
            ["---", "name: My Routine", "routine: true", "brain: none", "---", "Body."]
        ),
    )
    _write(
        config_dir / "commands/plain.md",
        "\n".join(["---", "name: Plain", "brain: none", "---", "Body."]),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    routine = {
        item.command: item for item in runtime.get_routine_command_options().options
    }

    assert "my_routine" in routine
    assert "plain" not in routine
    ticket = routine["workflows/ticket_driven_workflow"]
    assert ticket.source == "template"
    assert ticket.category == "workflow"
    assert ticket.routine_eligible is True


def test_routine_command_options_read_embedded_python_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/my_routine.py",
        "\n".join(
            [
                "COMMAND_METADATA = {",
                "    'name': {'en': 'My Routine', 'ja': '私の巡回'},",
                "    'description': {",
                "        'en': 'Run my routine.',",
                "        'ja': '私の巡回処理を実行します。',",
                "    },",
                "    'routine': True,",
                "}",
                "",
                "async def main(context):",
                '    """Fallback English description."""',
                "    return None",
            ]
        ),
    )
    context = _make_context([_make_person()], language_code="ja")
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "my_routine"
    )

    assert option.label == "私の巡回"
    assert option.description == "私の巡回処理を実行します。"
    assert option.routine_eligible is True


def test_routine_command_options_reuse_loaded_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    command_path = _write(
        config_dir / "commands/my_routine.md",
        "\n".join(
            ["---", "name: My Routine", "routine: true", "brain: none", "---", "Body."]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)
    original = runtime_module.load_command_metadata
    loads: list[Path] = []

    def counting_metadata(path: Path, language_code: str = "") -> dict[str, Any]:
        if path == command_path:
            loads.append(path)
        return original(path, language_code)

    monkeypatch.setattr(runtime_module, "load_command_metadata", counting_metadata)

    assert any(
        item.command == "my_routine"
        for item in runtime.get_routine_command_options().options
    )
    assert loads == [command_path]


def test_routine_command_options_localize_builtin_python_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    context = _make_context([_make_person()], language_code="ja")
    runtime = _runtime_with_context(monkeypatch, context)

    ticket = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "workflows/ticket_driven_workflow"
    )

    assert ticket.label == "チケット駆動ワークフロー"
    assert (
        ticket.description
        == "対応可能な GitHub issue または PR を1件取得し、AI CLIツールへ委譲します。"
    )


def test_routine_command_options_default_prefers_edition_when_multiple(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With several eligible candidates, the edition's declared default wins.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/my_routine.md",
        "\n".join(
            ["---", "name: My Routine", "routine: true", "brain: none", "---", "Body."]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    response = runtime.get_routine_command_options()

    assert len(response.options) > 1
    assert response.default_command == "workflows/ticket_driven_workflow"


def test_routine_command_options_default_is_sole_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With exactly one eligible candidate, it is the default on its own — the
    # edition's declared default is not consulted.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/workflows/ticket_driven_workflow.md",
        "\n".join(
            [
                "---",
                "name: Only Routine",
                "routine: true",
                "brain: none",
                "---",
                "Body.",
            ]
        ),
    )

    class EditionStub:
        def get_default_routines(self) -> list[str]:
            return ["workflows/some_other_default"]

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.get_edition", lambda: EditionStub()
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    response = runtime.get_routine_command_options()

    assert [option.command for option in response.options] == [
        "workflows/ticket_driven_workflow"
    ]
    assert response.default_command == "workflows/ticket_driven_workflow"


def test_routine_command_options_flag_ineligible_when_input_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared routine that still needs caller input stays listed but is
    # flagged ineligible rather than silently dropped.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/needs_input.md",
        "\n".join(
            ["---", "name: Needs Input", "routine: true", "---", "Summarize ${input}."]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "needs_input"
    )

    assert option.routine_eligible is False


def test_routine_command_options_accept_declared_argument_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/defaulted_input.md",
        "\n".join(
            [
                "---",
                "name: Defaulted Input",
                "routine: true",
                "args:",
                "  language:",
                "    default: English",
                "---",
                "Use ${language}.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "defaulted_input"
    )

    assert option.routine_eligible is True


def test_routine_command_options_flag_ineligible_when_message_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/needs_message.md",
        "\n".join(
            [
                "---",
                "name: Needs Message",
                "routine: true",
                "inputs:",
                "  message: required",
                "---",
                "Summarize the caller message.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "needs_message"
    )

    assert option.routine_eligible is False


def test_routine_command_options_ignore_hidden_defined_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/internal_input.md",
        "\n".join(
            [
                "---",
                "name: Internal Input",
                "routine: true",
                "inputs:",
                "  defined_args: hidden",
                "---",
                "Render ${internal_value} supplied by the workflow.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    option = next(
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "internal_input"
    )

    assert option.routine_eligible is True


def test_routine_command_options_prefer_workspace_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A workspace override of a built-in routine wins over the template copy and
    # keeps its routine declaration via the effective (highest-priority) file.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/workflows/ticket_driven_workflow.md",
        "\n".join(
            [
                "---",
                "name: Local Ticket",
                "routine: true",
                "brain: none",
                "---",
                "Body.",
            ]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    matches = [
        item
        for item in runtime.get_routine_command_options().options
        if item.command == "workflows/ticket_driven_workflow"
    ]

    assert len(matches) == 1
    assert matches[0].source == "workspace"
    assert matches[0].label == "Local Ticket"


def test_routine_command_options_exclude_general_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the routine catalog must not reuse the general command catalog.
    # An ordinary workspace command appears in /commands/options but never as a
    # routine candidate unless it declares itself one.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/summarize.md",
        "\n".join(
            ["---", "name: Summarize", "brain: none", "---", "Summarize ${file}."]
        ),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    plain = {item.command for item in runtime.get_command_options().options}
    routine = {item.command for item in runtime.get_routine_command_options().options}

    assert "summarize" in plain
    assert "summarize" not in routine


def test_routine_command_options_person_not_found_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    context = _make_context([_make_person("bot", "Bot")])
    runtime = _runtime_with_context(monkeypatch, context)

    with pytest.raises(AppApiError) as exc_info:
        runtime.get_routine_command_options(person="missing")

    assert exc_info.value.code == "person_not_found"


# ---------------------------------------------------------------------------
# author_command / run_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_author_command_uses_stable_authoring_identity_and_unique_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    ocr_source = "def main(context):\n    return context.pipe\n"
    _write(config_dir / "commands/ocr/extract-text.py", ocr_source)
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    captured: dict[str, Any] = {}

    async def fake_author_command_turn(context: object, **kwargs: Any) -> Any:
        captured["context"] = context
        captured.update(kwargs)
        captured["correlation"] = correlation_fields()
        return CommandAuthoringResult(
            action="propose_changes",
            message="Review the proposed change.",
            changes=[
                CommandAuthoringChange(
                    operation="update",
                    command="reports/weekly",
                    format="python",
                    content="def main(context):\n    return 'updated'\n",
                )
            ],
        )

    monkeypatch.setattr(runtime_module, "author_command_turn", fake_author_command_turn)

    response = await runtime.author_command(
        CommandAuthoringRequest(
            mode="edit",
            conversation_id="authoring-1",
            command="reports/weekly",
            format="python",
            content="old source",
            file_id="cmVwb3J0cy93ZWVrbHkucHk",
            revision="revision-1",
            message="Add a weekly report.",
            person="bot",
        )
    )

    assert response.message == "Review the proposed change."
    assert response.action == "propose_changes"
    assert response.changes[0].content == "def main(context):\n    return 'updated'\n"
    assert response.changes[0].relative_path == "reports/weekly.py"
    assert response.changes[0].file_id == "cmVwb3J0cy93ZWVrbHkucHk"
    assert captured["conversation_id"] == "authoring-1"
    assert captured["mode"] == "edit"
    assert captured["trace_id"] == response.trace_id
    assert captured["instruction"] == "Add a weekly report."
    assert {
        "command": "ocr/extract-text",
        "format": "python",
        "relative_path": "ocr/extract-text.py",
        "content": ocr_source,
    } in captured["available_commands"]
    assert captured["correlation"]["trace_id"] == response.trace_id
    # A Desktop-initiated turn is a manual run, so it is filterable in
    # diagnostics and stays off the activity timeline like other manual runs.
    assert captured["correlation"]["source"] == "manual"
    assert captured["correlation"]["command"] == "author:reports/weekly"
    assert captured["correlation"]["attributes"] == {
        "command_authoring.conversation_id": "authoring-1"
    }
    assert captured["context"].closed is True


@pytest.mark.asyncio
async def test_author_command_maps_work_rejection_to_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    def reject_work(**_: Any) -> Any:
        raise WorkRejectedError("busy", reason="lease_unavailable")

    monkeypatch.setattr(runtime._execution, "track_work", reject_work)

    with pytest.raises(AppApiError) as caught:
        await runtime.author_command(
            CommandAuthoringRequest(
                mode="create",
                conversation_id="authoring-1",
                message="Create a command.",
                person="bot",
            )
        )

    assert caught.value.status_code == 409
    assert caught.value.code == "work_rejected"


@pytest.mark.asyncio
async def test_author_command_maps_command_error_to_bad_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    async def fail_authoring(*_: Any, **__: Any) -> Any:
        raise CommandError("invalid agent response")

    monkeypatch.setattr(runtime_module, "author_command_turn", fail_authoring)

    with pytest.raises(AppApiError) as caught:
        await runtime.author_command(
            CommandAuthoringRequest(
                mode="create",
                conversation_id="authoring-1",
                message="Create a command.",
                person="bot",
            )
        )

    assert caught.value.status_code == 502
    assert caught.value.code == "command_authoring_failed"


@pytest.mark.asyncio
async def test_run_command_publishes_started_and_finished_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        return "output-value"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    response = await runtime.run_command(
        CommandRunRequest(command="demo", person="bot")
    )

    assert response.output == "output-value"
    events = event_bus.snapshot_events()
    assert [event["type"] for event in events] == [
        "command.started",
        "command.finished",
    ]
    assert events[0]["payload"] == {"command": "demo", "person": "bot"}
    assert events[1]["payload"] == {
        "command": "demo",
        "output_length": len("output-value"),
    }
    assert {event["trace_id"] for event in events} == {response.trace_id}
    assert {event["source"] for event in events} == {"manual"}


@pytest.mark.asyncio
async def test_run_command_passes_cwd_and_args_into_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = AppRuntime(EventBus())
    captured: dict[str, Any] = {}
    sentinel_context = _make_context([_make_person()])

    async def fake_run_command(self: object, context: object, **kwargs: Any) -> str:
        del self
        captured["context"] = context
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(runtime, "_get_context", lambda message="": sentinel_context)
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    await runtime.run_command(
        CommandRunRequest(
            command="demo",
            args=["one", "two"],
            person="bot",
            cwd=tmp_path,
        )
    )

    assert captured["context"] is sentinel_context
    assert captured["command_name"] == "demo"
    assert captured["command_args"] == ["one", "two"]
    assert captured["person_identifier"] == "bot"
    assert captured["cwd"] == tmp_path


@pytest.mark.asyncio
async def test_logs_during_run_command_carry_the_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Logs emitted while a manual command runs flow through the single log path
    # (EventBusLogHandler) and carry the run's trace id — replacing the old
    # duplicate command.log events.
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)
    guildbotics_logger = logging.getLogger("guildbotics")
    guildbotics_logger.setLevel(logging.INFO)
    log_handler = EventBusLogHandler(event_bus)
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    guildbotics_logger.addHandler(log_handler)
    log_sub = event_bus.subscribe_logs()

    async def fake_run_command(*_: Any, **__: Any) -> str:
        guildbotics_logger.info("progress message")
        return "done"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    try:
        response = await runtime.run_command(CommandRunRequest(command="demo"))
        item = await asyncio.wait_for(log_sub.get(), timeout=2.0)
    finally:
        guildbotics_logger.removeHandler(log_handler)
        log_sub.close()

    assert item["kind"] == "log"
    assert item["message"] == "progress message"
    assert item["trace_id"] == response.trace_id
    # No command.log events are produced anymore; only state-change events.
    assert {event["type"] for event in event_bus.snapshot_events()} == {
        "command.started",
        "command.finished",
    }


@pytest.mark.asyncio
async def test_run_command_publishes_failed_event_for_person_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)
    # No member can execute commands, so an omitted person has no default.
    context = _make_context(
        [
            Person(person_id="alice", name="alice", is_active=False),
            Person(person_id="bot", name="bot", is_active=False),
        ]
    )

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise AssertionError("The run must not start without a member.")

    monkeypatch.setattr(runtime, "_get_context", lambda message="": context)
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo"))

    assert exc_info.value.code == "person_selection_required"
    assert exc_info.value.context["available"] == ["alice", "bot"]
    failed = [
        event
        for event in event_bus.snapshot_events()
        if event["type"] == "command.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["payload"] == {
        "command": "demo",
        "code": "person_selection_required",
        "available": ["alice", "bot"],
    }


@pytest.mark.asyncio
async def test_run_command_publishes_failed_event_for_person_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise AssertionError("The run must not start for an unknown member.")

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo", person="ghost"))

    assert exc_info.value.code == "person_not_found"
    assert exc_info.value.context == {"identifier": "ghost", "available": ["bot"]}
    failed = [
        event
        for event in event_bus.snapshot_events()
        if event["type"] == "command.failed"
    ]
    assert failed[0]["payload"] == {
        "command": "demo",
        "code": "person_not_found",
        "identifier": "ghost",
        "available": ["bot"],
    }


@pytest.mark.asyncio
async def test_run_command_publishes_failed_event_for_command_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise CommandError("boom")

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo"))

    assert exc_info.value.code == "command_error"
    assert exc_info.value.message == "boom"
    failed = [
        event
        for event in event_bus.snapshot_events()
        if event["type"] == "command.failed"
    ]
    assert failed[0]["payload"] == {
        "command": "demo",
        "code": "command_error",
        "message": "boom",
    }


@pytest.mark.asyncio
async def test_run_command_publishes_failed_event_for_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise ValueError("unexpected")

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(ValueError, match="unexpected"):
        await runtime.run_command(CommandRunRequest(command="demo"))

    failed = [
        event
        for event in event_bus.snapshot_events()
        if event["type"] == "command.failed"
    ]
    assert failed[0]["payload"] == {"command": "demo", "error_type": "ValueError"}


@pytest.mark.asyncio
async def test_run_command_releases_reservation_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    attempts: list[str] = []

    async def fake_run_command(*_: Any, **__: Any) -> str:
        attempts.append("called")
        if len(attempts) == 1:
            raise CommandError("first failure")
        return "second ok"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(AppApiError):
        await runtime.run_command(CommandRunRequest(command="demo"))

    # Reservation must be released so a subsequent run is accepted (not 409).
    response = await runtime.run_command(CommandRunRequest(command="demo"))

    assert response.output == "second ok"
    assert attempts == ["called", "called"]


@pytest.mark.asyncio
async def test_run_command_releases_reservation_after_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    attempts: list[str] = []

    async def fake_run_command(*_: Any, **__: Any) -> str:
        attempts.append("called")
        if len(attempts) == 1:
            raise RuntimeError("crash")
        return "recovered"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    with pytest.raises(RuntimeError, match="crash"):
        await runtime.run_command(CommandRunRequest(command="demo"))

    response = await runtime.run_command(CommandRunRequest(command="demo"))

    assert response.output == "recovered"


@pytest.mark.asyncio
async def test_run_command_rejects_concurrent_run_with_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    # Simulate an in-flight command by holding the reservation.
    runtime._reserve_command("inflight-id")

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo"))

    assert exc_info.value.code == "command_already_running"
    assert exc_info.value.status_code == HTTP_CONFLICT
    assert exc_info.value.context == {"trace_id": "inflight-id"}


@pytest.mark.asyncio
async def test_run_command_rejects_person_lease_conflict_with_http_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _make_context([_make_person("bot")])
    runtime = _runtime_with_context(monkeypatch, context)
    calls: list[str] = []

    async def fake_run_command(*_: Any, **__: Any) -> str:
        calls.append("called")
        return "ok"

    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )
    holder = PersonExecutionLease("bot")
    holder.acquire(source="routine", command="ticket", work_id="existing-work")
    try:
        with pytest.raises(AppApiError) as exc_info:
            await runtime.run_command(CommandRunRequest(command="demo", person="bot"))
    finally:
        holder.release()

    assert exc_info.value.code == "work_rejected"
    assert exc_info.value.status_code == HTTP_CONFLICT
    assert calls == []

    response = await runtime.run_command(
        CommandRunRequest(command="demo", person="bot")
    )
    assert response.output == "ok"


@pytest.mark.asyncio
async def test_run_command_appears_in_runtime_active_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_run_command(*_: Any, **__: Any) -> str:
        started.set()
        await finish.wait()
        return "ok"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    task = asyncio.create_task(
        runtime.run_command(CommandRunRequest(command="demo", person="bot"))
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    status = runtime.get_scheduler_status()
    assert len(status.active_works) == 1
    active = status.active_works[0]
    assert active.source == "manual"
    assert active.person_id == "bot"
    assert active.command == "demo"

    finish.set()
    await task


@pytest.mark.asyncio
async def test_stop_scheduler_waits_for_manual_command_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_run_command(*_: Any, **__: Any) -> str:
        started.set()
        await finish.wait()
        return "ok"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    command_task = asyncio.create_task(
        runtime.run_command(CommandRunRequest(command="demo", person="bot"))
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    stop_task = asyncio.create_task(asyncio.to_thread(runtime.stop_scheduler))
    await asyncio.sleep(0.05)
    assert stop_task.done() is False

    finish.set()
    stopped = await asyncio.wait_for(stop_task, timeout=1.0)
    await command_task
    assert stopped.active_works == []


@pytest.mark.asyncio
async def test_force_stop_scheduler_cancels_manual_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus, stop_timeout_seconds=1.0)
    started = asyncio.Event()
    cancelled = {"value": False}

    async def fake_run_command(*_: Any, **__: Any) -> str:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return "unreachable"

    monkeypatch.setattr(
        runtime, "_get_context", lambda message="": _make_context([_make_person()])
    )
    monkeypatch.setattr(
        "guildbotics.app_api.runtime.LocalCommandExecutor.run", fake_run_command
    )

    command_task = asyncio.create_task(
        runtime.run_command(CommandRunRequest(command="demo", person="bot"))
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)

    stopped = await asyncio.to_thread(runtime.stop_scheduler, force=True)

    with pytest.raises(asyncio.CancelledError):
        await command_task
    assert cancelled["value"] is True
    assert stopped.active_works == []
    assert event_bus.snapshot_events()[-1]["payload"]["code"] == "cancelled"


# ---------------------------------------------------------------------------
# start_scheduler
# ---------------------------------------------------------------------------


def test_start_scheduler_maps_service_lock_conflict_to_http_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AppRuntime(EventBus())
    metadata = ServiceLockMetadata(
        pid=4242,
        service_instance_id="service-1",
        owner="cli",
        workspace="/tmp/other-workspace",
        started_at="2026-07-12T10:00:00+09:00",
    )

    def reject_start(_request: SchedulerStartRequest) -> None:
        raise ServiceLockUnavailableError(metadata)

    monkeypatch.setattr(runtime._lifecycle, "start", reject_start)

    with pytest.raises(AppApiError) as caught:
        runtime.start_scheduler(SchedulerStartRequest())

    assert caught.value.code == "service_already_running"
    assert caught.value.status_code == HTTP_CONFLICT
    assert caught.value.context == {
        "owner": "cli",
        "pid": 4242,
        "workspace": "/tmp/other-workspace",
        "started_at": "2026-07-12T10:00:00+09:00",
    }


# ---------------------------------------------------------------------------
# Command file execution status / run target guarantee
# ---------------------------------------------------------------------------


def _file_reference(config_dir: Path, relative: str) -> tuple[str, str]:
    """Return the ``(file_id, revision)`` for a shared command file."""
    path = config_dir / "commands" / relative
    return encode_file_id(relative), file_revision(path.read_bytes())


def test_execution_status_matches_effective_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Greet", "brain: none", "---", "Body."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")

    status = runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert status.matches_selected_file is True
    assert status.blocking_code is None


def test_execution_status_reports_revision_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Greet", "brain: none", "---", "Body."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, _ = _file_reference(config_dir, "greet.md")

    status = runtime.get_command_file_execution_status(file_id, "bot", "stale")

    assert status.matches_selected_file is False
    assert status.blocking_code == "command_file_changed"


def test_execution_status_blocks_invalid_saved_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/broken.md",
        "---\nargs:\n  - name: text\n---\nBody.\n",
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "broken.md")

    status = runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert status.matches_selected_file is False
    assert status.blocking_code == "command_file_invalid_source"
    assert status.blocking_context["message"] == "Command 'args' must be a mapping."


def test_execution_status_blocks_non_utf8_saved_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    path = config_dir / "commands/broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff")
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "broken.md")

    status = runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert status.matches_selected_file is False
    assert status.blocking_code == "command_file_invalid_source"
    assert status.blocking_context["reason"] == "invalid_utf8"


def test_execution_status_validates_the_revision_checked_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    path = config_dir / "commands/greet.md"
    _write(path, "---\nbrain: none\n---\nBody.\n")
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")
    read_bytes = Path.read_bytes

    def read_then_replace(target: Path) -> bytes:
        data = read_bytes(target)
        if target == path:
            target.write_text(
                "---\nbrain: none\nargs:\n  - text\n---\nBroken.\n",
                encoding="utf-8",
            )
        return data

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)

    status = runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert status.matches_selected_file is True
    assert status.blocking_code is None


def test_run_guard_rejects_invalid_saved_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/broken.md",
        "---\nargs:\n  - name: text\n---\nBody.\n",
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "broken.md")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="broken",
                    person="bot",
                    expected_command_file_id=file_id,
                    expected_command_file_revision=revision,
                )
            )
        )

    assert caught.value.code == "command_file_invalid_source"


def test_execution_status_member_override_is_shadowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Shared", "brain: none", "---", "Shared."]),
    )
    _write(
        config_dir / "team/members/bot/commands/greet.md",
        "\n".join(["---", "name: Member", "brain: none", "---", "Member."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")

    status = runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert status.blocking_code == "command_file_shadowed"
    assert status.blocking_context["shadow_source"] == "member"


def test_execution_status_rejects_shadowed_file_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Md", "brain: none", "---", "Body."]),
    )
    _write(config_dir / "commands/greet.yaml", "commands: []\n")
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    # The lower-priority .yaml file is not the effective shared file, so it is
    # not editable / inspectable through a crafted id.
    file_id, revision = _file_reference(config_dir, "greet.yaml")

    with pytest.raises(AppApiError) as caught:
        runtime.get_command_file_execution_status(file_id, "bot", revision)

    assert caught.value.code == "command_file_not_found"


def test_run_command_rejects_command_id_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A valid file id/revision for command A must not be usable to run a
    # different command B.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Greet", "brain: none", "---", "Body."]),
    )
    _write(
        config_dir / "commands/other.md",
        "\n".join(["---", "name: Other", "brain: none", "---", "Body."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="other",
                    person="bot",
                    expected_command_file_id=file_id,
                    expected_command_file_revision=revision,
                )
            )
        )

    assert caught.value.code == "command_file_shadowed"


def test_run_command_rejects_missing_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An expected-file run must also be blocked when a requirement is missing,
    # even though the frontend may briefly enable the button.
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/needs_github.py",
        "\n".join(
            [
                "from guildbotics.integrations.ticket_manager import TicketManager",
                "",
                "async def main(context):",
                "    return ''",
            ]
        ),
    )
    runtime = _runtime_with_context(
        monkeypatch, _make_context([_make_person()], github_enabled=False)
    )
    file_id, revision = _file_reference(config_dir, "needs_github.py")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="needs_github",
                    person="bot",
                    expected_command_file_id=file_id,
                    expected_command_file_revision=revision,
                )
            )
        )

    assert caught.value.code == "command_requirement_missing"


def test_execution_status_person_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Greet", "brain: none", "---", "Body."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")

    status = runtime.get_command_file_execution_status(file_id, "ghost", revision)

    assert status.blocking_code == "person_not_found"


def test_run_command_rejects_revision_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Greet", "brain: none", "---", "Body."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, _ = _file_reference(config_dir, "greet.md")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="greet",
                    person="bot",
                    expected_command_file_id=file_id,
                    expected_command_file_revision="stale",
                )
            )
        )

    assert caught.value.code == "command_file_changed"
    assert caught.value.status_code == HTTP_CONFLICT


def test_run_command_rejects_member_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Shared", "brain: none", "---", "Shared."]),
    )
    _write(
        config_dir / "team/members/bot/commands/greet.md",
        "\n".join(["---", "name: Member", "brain: none", "---", "Member."]),
    )
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    file_id, revision = _file_reference(config_dir, "greet.md")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="greet",
                    person="bot",
                    expected_command_file_id=file_id,
                    expected_command_file_revision=revision,
                )
            )
        )

    assert caught.value.code == "command_file_shadowed"
    assert caught.value.context["shadow_source"] == "member"


def test_execution_status_uses_the_default_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a person the status must describe the default member's run."""
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Shared", "brain: none", "---", "Shared."]),
    )
    _write(
        config_dir / "team/members/bot/commands/greet.md",
        "\n".join(["---", "name: Member", "brain: none", "---", "Member."]),
    )
    context = _make_context(
        [_make_person()],
        person=Person(person_id="default_person", name="Default Person"),
    )
    runtime = _runtime_with_context(monkeypatch, context)
    file_id, revision = _file_reference(config_dir, "greet.md")

    status = runtime.get_command_file_execution_status(file_id, None, revision)

    assert status.blocking_code == "command_file_shadowed"
    assert status.blocking_context["shadow_source"] == "member"


def test_run_command_guards_the_file_of_the_default_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An omitted person must guard the file the default member actually runs.

    Otherwise the guard checks the shared file while the run executes the
    default member's same-named command file.
    """
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/greet.md",
        "\n".join(["---", "name: Shared", "brain: none", "---", "Shared."]),
    )
    _write(
        config_dir / "team/members/bot/commands/greet.md",
        "\n".join(["---", "name: Member", "brain: none", "---", "Member."]),
    )
    context = _make_context(
        [_make_person()],
        person=Person(person_id="default_person", name="Default Person"),
    )
    runtime = _runtime_with_context(monkeypatch, context)
    file_id, revision = _file_reference(config_dir, "greet.md")

    with pytest.raises(AppApiError) as caught:
        asyncio.run(
            runtime.run_command(
                CommandRunRequest(
                    command="greet",
                    expected_command_file_id=file_id,
                    expected_command_file_revision=revision,
                )
            )
        )

    assert caught.value.code == "command_file_shadowed"
    assert caught.value.context["shadow_source"] == "member"


# ---------------------------------------------------------------------------
# troubleshoot
# ---------------------------------------------------------------------------


def _troubleshooting_runtime(
    monkeypatch: pytest.MonkeyPatch, recorded_trace_ids: set[str]
) -> AppRuntime:
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))

    class _StoreStub:
        def get_summary(self, trace_id: str) -> dict[str, Any] | None:
            return {"trace_id": trace_id} if trace_id in recorded_trace_ids else None

    runtime._diagnostics_store = _StoreStub()  # type: ignore[assignment]
    return runtime


@pytest.mark.asyncio
async def test_troubleshoot_scopes_the_turn_and_returns_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, {"abc123"})
    captured: dict[str, Any] = {}

    async def fake_turn(context: object, **kwargs: Any) -> Any:
        captured["context"] = context
        captured.update(kwargs)
        captured["correlation"] = correlation_fields()
        return TroubleshootingResult(message="The token expired.", trace_ids=["abc123"])

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fake_turn)

    response = await runtime.troubleshoot(
        TroubleshootingRequest(
            conversation_id="conv-1",
            message="Why did this fail?",
            person="bot",
            focus=TroubleshootingFocus(view="trace", trace_id="abc123"),
        )
    )

    assert response.message == "The token expired."
    assert response.trace_ids == ["abc123"]
    assert captured["question"] == "Why did this fail?"
    assert captured["conversation_id"] == "conv-1"
    assert captured["trace_id"] == response.trace_id
    assert captured["focus"]["trace_id"] == "abc123"
    assert captured["correlation"]["source"] == "manual"
    assert captured["correlation"]["command"] == "troubleshoot:abc123"
    assert captured["correlation"]["attributes"] == {
        "troubleshooting.conversation_id": "conv-1"
    }
    assert captured["context"].closed is True


@pytest.mark.asyncio
async def test_troubleshoot_drops_trace_ids_that_were_never_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, {"real"})

    async def fake_turn(*_: Any, **__: Any) -> Any:
        return TroubleshootingResult(message="…", trace_ids=["real", "invented", ""])

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fake_turn)

    response = await runtime.troubleshoot(
        TroubleshootingRequest(conversation_id="conv-1", message="Why?", person="bot")
    )

    # Every reference becomes a link, so an invented trace id must not survive.
    assert response.trace_ids == ["real"]


@pytest.mark.asyncio
async def test_author_command_is_read_only_and_skips_the_member_execution_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _runtime_with_context(monkeypatch, _make_context([_make_person()]))
    exclusivity: dict[str, Any] = {}
    original = runtime._execution.track_work

    def record_exclusive(**kwargs: Any) -> Any:
        exclusivity["exclusive"] = kwargs.get("exclusive", True)
        return original(**kwargs)

    monkeypatch.setattr(runtime._execution, "track_work", record_exclusive)

    async def fake_author(*_: Any, **__: Any) -> Any:
        return CommandAuthoringResult(
            action="answer", message="This is possible.", changes=[]
        )

    monkeypatch.setattr(runtime_module, "author_command_turn", fake_author)

    await runtime.author_command(
        CommandAuthoringRequest(
            mode="create", conversation_id="c", message="Create it.", person="bot"
        )
    )

    assert exclusivity["exclusive"] is False


@pytest.mark.asyncio
async def test_troubleshoot_runs_while_the_member_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())

    async def fake_turn(*_: Any, **__: Any) -> Any:
        return TroubleshootingResult(message="…")

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fake_turn)

    lease = PersonExecutionLease("bot")
    lease.acquire(source="routine", command="workflows/ticket", work_id="other")
    try:
        response = await runtime.troubleshoot(
            TroubleshootingRequest(
                conversation_id="conv-1", message="Why?", person="bot"
            )
        )
    finally:
        lease.release()

    assert response.message == "…"


@pytest.mark.asyncio
async def test_troubleshoot_maps_assistant_failure_to_bad_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())

    async def fail(*_: Any, **__: Any) -> Any:
        raise CommandError("invalid agent response")

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fail)

    with pytest.raises(AppApiError) as caught:
        await runtime.troubleshoot(
            TroubleshootingRequest(
                conversation_id="conv-1", message="Why?", person="bot"
            )
        )

    assert caught.value.status_code == 502
    assert caught.value.code == "troubleshooting_failed"


@pytest.mark.asyncio
async def test_troubleshoot_maps_cli_agent_failure_to_bad_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An AI CLI tool that exits non-zero must reach the panel as its own reason."""
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())

    async def fail(*_: Any, **__: Any) -> Any:
        raise CliAgentExecutionError(
            cli_agent="default",
            result=CliAgentExecutionResult(
                stdout="", stderr="not logged in", returncode=1
            ),
        )

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fail)

    with pytest.raises(AppApiError) as caught:
        await runtime.troubleshoot(
            TroubleshootingRequest(
                conversation_id="conv-1", message="Why?", person="bot"
            )
        )

    assert caught.value.status_code == 502
    assert caught.value.code == "troubleshooting_failed"
    assert "not logged in" in caught.value.message


@pytest.mark.asyncio
async def test_troubleshoot_lets_unexpected_defects_stay_internal_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A defect inside the turn must not be dressed up as an agent failure."""
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())

    async def fail(*_: Any, **__: Any) -> Any:
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fail)

    with pytest.raises(TypeError):
        await runtime.troubleshoot(
            TroubleshootingRequest(
                conversation_id="conv-1", message="Why?", person="bot"
            )
        )


@pytest.mark.asyncio
async def test_troubleshoot_keeps_the_app_api_error_a_turn_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An already-typed failure keeps its code and status instead of becoming 502."""
    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())

    async def fail(*_: Any, **__: Any) -> Any:
        raise AppApiError(
            "person_not_found", "Person 'bot' not found.", status_code=400
        )

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", fail)

    with pytest.raises(AppApiError) as caught:
        await runtime.troubleshoot(
            TroubleshootingRequest(
                conversation_id="conv-1", message="Why?", person="bot"
            )
        )

    assert caught.value.status_code == 400
    assert caught.value.code == "person_not_found"


@pytest.mark.asyncio
async def test_assistant_turns_are_recorded_as_manual_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both assistants must be filterable, and neither may reach activity."""
    from guildbotics.app_api.activity_history import MANUAL_SESSION_SOURCE

    _isolate_workspace(tmp_path, monkeypatch)
    runtime = _troubleshooting_runtime(monkeypatch, set())
    sources: list[str] = []

    async def capture_troubleshoot(*_: Any, **__: Any) -> Any:
        sources.append(str(correlation_fields()["source"]))
        return TroubleshootingResult(message="…")

    async def capture_author(*_: Any, **__: Any) -> Any:
        sources.append(str(correlation_fields()["source"]))
        return CommandAuthoringResult(
            action="answer", message="This is possible.", changes=[]
        )

    monkeypatch.setattr(runtime_module, "troubleshoot_turn", capture_troubleshoot)
    monkeypatch.setattr(runtime_module, "author_command_turn", capture_author)

    await runtime.troubleshoot(
        TroubleshootingRequest(conversation_id="c", message="Why?", person="bot")
    )
    await runtime.author_command(
        CommandAuthoringRequest(
            mode="create", conversation_id="c", message="Create it.", person="bot"
        )
    )

    # "manual" is both a source the diagnostics screen offers as a filter and
    # the one activity history excludes, which is what these turns need.
    assert sources == [MANUAL_SESSION_SOURCE, MANUAL_SESSION_SOURCE]
