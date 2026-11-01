"""Unit tests for ``guildbotics.app_api.models`` and the request models that
implement the behaviors called out in the test-gap analysis.

The gap analysis references ``MemberTaskSchedule`` and
``MemberConfigUpdateRequest``; those names do not exist in the codebase. The
described behaviors (five-field cron validation, blank-schedule exclusion and
optional secret empty-string handling) are implemented by
``PersonTaskScheduleInput`` and the project/person update inputs in
``guildbotics.setup.setup_service``, so they are exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.app_api.models import (
    CommandAuthoringRequest,
    CommandOption,
    CommandRunRequest,
    ProjectConfigUpdateRequest,
    RuntimeActiveWork,
    RuntimeStatus,
    RuntimeUnitStatus,
)
from guildbotics.setup.setup_service import PersonTaskScheduleInput


def test_command_authoring_edit_requires_file_revision_identity() -> None:
    with pytest.raises(ValidationError):
        CommandAuthoringRequest(
            mode="edit",
            conversation_id="conversation",
            message="Update it.",
            command="translate",
            format="markdown",
            content="source",
        )


def test_command_authoring_create_rejects_existing_draft_fields() -> None:
    with pytest.raises(ValidationError):
        CommandAuthoringRequest(
            mode="create",
            conversation_id="conversation",
            message="Create it.",
            content="source",
        )


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


# --- Path JSON serialization -----------------------------------------------

# The value names a directory on the device the Desktop is running on, so it
# is compared as that device spells it.


def test_command_run_request_serializes_path_to_json_string() -> None:
    request = CommandRunRequest(command="hello", cwd=Path("/tmp/work dir"))
    payload = json.loads(request.model_dump_json())
    assert payload["cwd"] == str(Path("/tmp/work dir"))
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
    assert payload["path"] == str(Path("/cfg/commands/hello.md"))
    assert isinstance(payload["path"], str)


def test_command_run_request_requires_non_empty_command() -> None:
    with pytest.raises(ValidationError):
        CommandRunRequest(command="")


# --- provider_api_keys handling --------------------------------------------


def test_project_config_update_request_provider_api_keys_default_empty() -> None:
    request = ProjectConfigUpdateRequest(
        config_dir=Path("/cfg"),
        language="en",
        llm_api_type="openai",
        cli_agent="codex",
    )
    assert request.provider_api_keys == {}
    assert request.services == {}


def test_project_config_update_request_keeps_provider_api_keys() -> None:
    request = ProjectConfigUpdateRequest(
        config_dir=Path("/cfg"),
        language="en",
        llm_api_type="openai",
        cli_agent="codex",
        provider_api_keys={"openai": "token", "gemini": ""},
    )
    assert request.provider_api_keys == {"openai": "token", "gemini": ""}


def test_project_config_update_request_serializes_path_to_json_string() -> None:
    request = ProjectConfigUpdateRequest(config_dir=Path("/cfg"), language="en")
    payload = json.loads(request.model_dump_json())
    assert payload["config_dir"] == str(Path("/cfg"))
    assert "env_file_path" not in payload


def _unit(target, state="stopped", running=False) -> RuntimeUnitStatus:
    return RuntimeUnitStatus(target=target, state=state, running=running)


_MANUAL_WORK = RuntimeActiveWork(
    id="w1",
    source="manual",
    person_id="yuki",
    command="ask",
    started_at="2026-09-17T00:00:00Z",
)


@pytest.mark.parametrize(
    ("scheduler", "events", "active_works", "expected"),
    [
        (_unit("scheduler"), _unit("events"), [], False),
        (_unit("scheduler", "failed"), _unit("events", "failed"), [], False),
        (_unit("scheduler", "running", True), _unit("events"), [], True),
        (_unit("scheduler"), _unit("events", "running", True), [], True),
        (_unit("scheduler"), _unit("events"), [_MANUAL_WORK], True),
        (_unit("scheduler", "stopping"), _unit("events"), [], True),
        (_unit("scheduler"), _unit("events", "stopping"), [], True),
    ],
)
def test_runtime_status_reports_active_work(
    scheduler, events, active_works, expected
) -> None:
    status = RuntimeStatus(
        scheduler=scheduler, events=events, active_works=active_works
    )

    assert status.has_active_work is expected
    # Serialized too: the desktop and its host read it from the response.
    assert status.model_dump()["has_active_work"] is expected
