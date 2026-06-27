"""Unit tests for ``guildbotics.app_api.models`` and the request models that
implement the behaviors called out in the test-gap analysis.

The gap analysis references ``MemberTaskSchedule`` and
``MemberConfigUpdateRequest``; those names do not exist in the codebase. The
described behaviors (five-field cron validation, blank-schedule exclusion and
optional secret empty-string handling) are implemented by
``PersonTaskScheduleInput`` and the project/person update inputs in
``guildbotics.editions.simple.setup_service``, so they are exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.app_api.models import (
    CommandOption,
    CommandRunRequest,
    ProjectConfigUpdateRequest,
    SchedulerStartRequest,
)
from guildbotics.editions.simple.setup_service import (
    PersonTaskScheduleInput,
    ProjectUpdateInput,
)

DEFAULT_MAX_CONSECUTIVE_ERRORS = 3
DEFAULT_ROUTINE_INTERVAL_MINUTES = 10


def _project_update_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "config_dir": Path("/cfg"),
        "env_file_path": Path("/cfg/.env"),
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "github_enabled": False,
    }
    base.update(overrides)
    return base


# --- PersonTaskScheduleInput (five-field cron / blank handling) -------------


def test_task_schedule_accepts_five_field_cron() -> None:
    schedule = PersonTaskScheduleInput(command="run-it", schedules=["0 9 * * 1"])
    assert schedule.command == "run-it"
    assert schedule.schedules == ["0 9 * * 1"]


@pytest.mark.parametrize(
    "expression",
    [
        "0 9 * * 1 7",  # six fields
        "0 9 * *",  # four fields
        "* * * *",  # four fields
        "*",  # one field
    ],
)
def test_task_schedule_rejects_non_five_field_cron(expression: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        PersonTaskScheduleInput(command="run-it", schedules=[expression])
    assert "five-field cron expression" in str(exc_info.value)


def test_task_schedule_excludes_blank_schedules() -> None:
    schedule = PersonTaskScheduleInput(
        command="run-it",
        schedules=["   ", "", "0 9 * * 1", "\t"],
    )
    # Blank / whitespace-only entries are dropped, valid ones are stripped.
    assert schedule.schedules == ["0 9 * * 1"]


def test_task_schedule_strips_surrounding_whitespace() -> None:
    schedule = PersonTaskScheduleInput(
        command="  run-it  ", schedules=["  0 9 * * 1  "]
    )
    assert schedule.command == "run-it"
    assert schedule.schedules == ["0 9 * * 1"]


def test_task_schedule_requires_command() -> None:
    with pytest.raises(ValidationError) as exc_info:
        PersonTaskScheduleInput(command="   ", schedules=[])
    assert "command is required" in str(exc_info.value)


# --- SchedulerStartRequest validation --------------------------------------


def test_scheduler_start_request_defaults() -> None:
    request = SchedulerStartRequest()
    assert request.sources.scheduled is True
    assert request.sources.routine is True
    assert request.sources.event_queue is True
    assert request.routine_commands == []
    assert request.max_consecutive_errors == DEFAULT_MAX_CONSECUTIVE_ERRORS
    assert request.routine_interval_minutes == DEFAULT_ROUTINE_INTERVAL_MINUTES


def test_scheduler_start_request_accepts_source_selection() -> None:
    request = SchedulerStartRequest(
        sources={"scheduled": False, "routine": False, "event_queue": True}
    )
    assert request.sources.scheduled is False
    assert request.sources.routine is False
    assert request.sources.event_queue is True


def test_scheduler_start_request_rejects_empty_source_selection() -> None:
    with pytest.raises(ValidationError):
        SchedulerStartRequest(
            sources={"scheduled": False, "routine": False, "event_queue": False}
        )


@pytest.mark.parametrize("value", [0, -1, -100])
def test_scheduler_start_request_rejects_non_positive_max_errors(value: int) -> None:
    with pytest.raises(ValidationError):
        SchedulerStartRequest(max_consecutive_errors=value)


@pytest.mark.parametrize("value", [0, -1, -5])
def test_scheduler_start_request_rejects_non_positive_interval(value: int) -> None:
    with pytest.raises(ValidationError):
        SchedulerStartRequest(routine_interval_minutes=value)


def test_scheduler_start_request_accepts_minimum_values() -> None:
    request = SchedulerStartRequest(
        max_consecutive_errors=1, routine_interval_minutes=1
    )
    assert request.max_consecutive_errors == 1
    assert request.routine_interval_minutes == 1


# --- Path JSON serialization -----------------------------------------------


def test_command_run_request_serializes_path_to_json_string() -> None:
    request = CommandRunRequest(command="hello", cwd=Path("/tmp/work dir"))
    payload = json.loads(request.model_dump_json())
    assert payload["cwd"] == "/tmp/work dir"
    assert isinstance(payload["cwd"], str)


def test_command_run_request_serializes_none_cwd() -> None:
    request = CommandRunRequest(command="hello")
    payload = json.loads(request.model_dump_json())
    assert payload["cwd"] is None


def test_command_option_serializes_path_to_json_string() -> None:
    option = CommandOption(
        command="hello",
        label="Hello",
        category="custom",
        source="workspace",
        path=Path("/cfg/commands/hello.md"),
    )
    payload = json.loads(option.model_dump_json())
    assert payload["path"] == "/cfg/commands/hello.md"
    assert isinstance(payload["path"], str)


def test_command_run_request_requires_non_empty_command() -> None:
    with pytest.raises(ValidationError):
        CommandRunRequest(command="")


# --- Optional secret empty-string handling ---------------------------------


def test_project_config_update_request_secrets_default_to_none() -> None:
    request = ProjectConfigUpdateRequest(
        config_dir=Path("/cfg"),
        env_file_path=Path("/cfg/.env"),
        language="en",
        llm_api_type="openai",
        cli_agent="codex",
        github_enabled=False,
    )
    assert request.google_api_key is None
    assert request.openai_api_key is None
    assert request.anthropic_api_key is None


def test_project_config_update_request_preserves_empty_string_secret() -> None:
    request = ProjectConfigUpdateRequest(
        config_dir=Path("/cfg"),
        env_file_path=Path("/cfg/.env"),
        language="en",
        llm_api_type="openai",
        cli_agent="codex",
        github_enabled=False,
        openai_api_key="",
        google_api_key="value",
    )
    # Empty string is preserved distinctly from None (callers treat "" as
    # "clear the secret" and None as "leave unchanged").
    assert request.openai_api_key == ""
    assert request.google_api_key == "value"
    assert request.anthropic_api_key is None


def test_project_update_input_secrets_default_to_none() -> None:
    request = ProjectUpdateInput(**_project_update_kwargs())
    assert request.google_api_key is None
    assert request.openai_api_key is None
    assert request.anthropic_api_key is None


def test_project_update_input_preserves_empty_string_secret() -> None:
    request = ProjectUpdateInput(
        **_project_update_kwargs(anthropic_api_key="", openai_api_key="token")
    )
    assert request.anthropic_api_key == ""
    assert request.openai_api_key == "token"
    assert request.google_api_key is None


def test_project_update_input_serializes_path_to_json_string() -> None:
    request = ProjectUpdateInput(**_project_update_kwargs())
    payload = json.loads(request.model_dump_json())
    assert payload["config_dir"] == "/cfg"
    assert payload["env_file_path"] == "/cfg/.env"
