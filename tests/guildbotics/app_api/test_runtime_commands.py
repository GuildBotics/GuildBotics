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
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.events import EventBus, EventBusLogHandler
from guildbotics.app_api.models import (
    CommandRunRequest,
    SchedulerStartRequest,
)
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.commands.errors import (
    CommandError,
    PersonNotFoundError,
    PersonSelectionRequiredError,
)
from guildbotics.runtime.person_lease import PersonExecutionLease
from guildbotics.runtime.service_lock import (
    ServiceLockMetadata,
    ServiceLockUnavailableError,
)

HTTP_BAD_REQUEST = 400
HTTP_CONFLICT = 409


def _make_person(person_id: str = "bot", name: str = "Bot") -> object:
    return type("PersonStub", (), {"person_id": person_id, "name": name})()


def _make_context(
    members: list[object],
    *,
    language_code: str = "en",
    github_enabled: bool = False,
) -> object:
    person = members[0]
    project = type(
        "ProjectStub",
        (),
        {
            "get_language_code": lambda self: language_code,
            "is_available_service": lambda self, service: github_enabled,
        },
    )()
    team = type("TeamStub", (), {"project": project, "members": members})()

    def clone_for(self: object, selected: object) -> object:
        return _make_context(
            [selected],
            language_code=language_code,
            github_enabled=github_enabled,
        )

    return type(
        "ContextStub",
        (),
        {"team": team, "person": person, "clone_for": clone_for},
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
    assert option.recommended_input == "args"
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
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    options = {item.command: item for item in runtime.get_command_options().options}

    assert {req.kind for req in options["llm_task"].requirements} == {"llm"}
    assert {req.kind for req in options["cli_task"].requirements} == {"cli_agent"}


def test_command_options_ignore_invalid_metadata_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    # Broken YAML, unparseable Python and broken sidecar metadata must not abort
    # discovery.
    _write(config_dir / "commands/broken.yml", "::: not: valid: yaml:::\n- [")
    _write(config_dir / "commands/broken.py", "def main(:\n    pass")
    _write(config_dir / "commands/ok.metadata.yml", "::: not: valid: yaml:::\n- [")
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


def test_command_options_exclude_sidecar_metadata_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/report.py",
        "\n".join(
            ["async def main(context):", '    """Run a report."""', "    return None"]
        ),
    )
    _write(
        config_dir / "commands/report.metadata.yml",
        "\n".join(["name: Report", "description: Report metadata."]),
    )
    context = _make_context([_make_person()])
    runtime = _runtime_with_context(monkeypatch, context)

    options = {item.command for item in runtime.get_command_options().options}

    assert "report" in options
    assert "report.metadata" not in options


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
    # routine metadata in a sidecar file) is discovered through the same single
    # pass.
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


def test_routine_command_options_read_python_sidecar_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/my_routine.py",
        "\n".join(
            [
                "async def main(context):",
                '    """Fallback English description."""',
                "    return None",
            ]
        ),
    )
    _write(
        config_dir / "commands/my_routine.metadata.yml",
        "\n".join(
            [
                "name:",
                "  en: My Routine",
                "  ja: 私の巡回",
                "description:",
                "  en: Run my routine.",
                "  ja: 私の巡回処理を実行します。",
                "routine: true",
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
    original = runtime_module._command_metadata
    loads: list[Path] = []

    def counting_metadata(path: Path, language_code: str = "") -> dict[str, Any]:
        if path == command_path:
            loads.append(path)
        return original(path, language_code)

    monkeypatch.setattr(runtime_module, "_command_metadata", counting_metadata)

    assert any(
        item.command == "my_routine"
        for item in runtime.get_routine_command_options().options
    )
    assert loads == [command_path]


def test_routine_command_options_do_not_reload_duplicate_en_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = _isolate_workspace(tmp_path, monkeypatch)
    _write(
        config_dir / "commands/my_routine.py",
        "\n".join(["async def main(context):", "    return None"]),
    )
    sidecar_path = _write(
        config_dir / "commands/my_routine.metadata.en.yml",
        "\n".join(["name: My Routine", "routine: true"]),
    )
    context = _make_context([_make_person()], language_code="en")
    runtime = _runtime_with_context(monkeypatch, context)
    original = runtime_module.load_yaml_file
    loads: list[Path] = []

    def counting_load_yaml_file(path: Path) -> Any:
        if path == sidecar_path:
            loads.append(path)
        return original(path)

    monkeypatch.setattr(runtime_module, "load_yaml_file", counting_load_yaml_file)

    assert any(
        item.command == "my_routine"
        for item in runtime.get_routine_command_options().options
    )
    assert loads == [sidecar_path]


def test_routine_command_options_localize_builtin_sidecar_metadata(
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
# run_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_command_publishes_started_and_finished_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        return "output-value"

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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
    sentinel_context = object()

    async def fake_run_command(context: object, **kwargs: Any) -> str:
        captured["context"] = context
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(runtime, "_get_context", lambda message="": sentinel_context)
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise PersonSelectionRequiredError(["bot", "alice"])

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo"))

    assert exc_info.value.code == "person_selection_required"
    assert exc_info.value.context["available"] == ["bot", "alice"]
    failed = [
        event
        for event in event_bus.snapshot_events()
        if event["type"] == "command.failed"
    ]
    assert len(failed) == 1
    assert failed[0]["payload"] == {
        "command": "demo",
        "code": "person_selection_required",
        "available": ["bot", "alice"],
    }


@pytest.mark.asyncio
async def test_run_command_publishes_failed_event_for_person_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_bus = EventBus()
    runtime = AppRuntime(event_bus)

    async def fake_run_command(*_: Any, **__: Any) -> str:
        raise PersonNotFoundError("ghost", ["bot"])

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())

    with pytest.raises(AppApiError) as exc_info:
        await runtime.run_command(CommandRunRequest(command="demo"))

    assert exc_info.value.code == "command_already_running"
    assert exc_info.value.status_code == HTTP_CONFLICT
    assert exc_info.value.context == {"trace_id": "inflight-id"}


@pytest.mark.asyncio
async def test_run_command_rejects_person_lease_conflict_with_http_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(data_root))
    context = _make_context([_make_person("bot")])
    runtime = _runtime_with_context(monkeypatch, context)
    calls: list[str] = []

    async def fake_run_command(*_: Any, **__: Any) -> str:
        calls.append("called")
        return "ok"

    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)
    holder = PersonExecutionLease("bot", data_root)
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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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

    monkeypatch.setattr(runtime, "_get_context", lambda message="": object())
    monkeypatch.setattr("guildbotics.app_api.runtime.run_command", fake_run_command)

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
