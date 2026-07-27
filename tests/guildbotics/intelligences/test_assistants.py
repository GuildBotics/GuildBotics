"""Tests for the shared structured-assistant turn contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from guildbotics.commands.errors import CommandError
from guildbotics.intelligences.assistants import (
    AssistantResponseError,
    open_assistant_session,
)


class _Reply(BaseModel):
    message: str


class _BrainStub:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies) or [_Reply(message="ok")]
        self.messages: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def run(self, message: str, **kwargs: Any) -> Any:
        self.messages.append(message)
        self.kwargs.append(kwargs)
        return self.replies[min(len(self.messages) - 1, len(self.replies) - 1)]


class _ContextStub:
    def __init__(self, brain: Any) -> None:
        self.brain = brain
        self.requested: list[str] = []

    def get_brain(self, name: str, config: None, class_resolver: None) -> Any:
        self.requested.append(name)
        assert config is None
        assert class_resolver is None
        return self.brain


def _open(context: Any, data_root: Path) -> Any:
    return open_assistant_session(
        context,
        prompt="functions/demo",
        work_kind="demo_kind",
        conversation_id="conv-1",
        trace_id="trace-1",
        result_type=_Reply,
        workspace_data_root=data_root,
        cwd_name="demo",
    )


@pytest.mark.asyncio
async def test_session_sends_json_payload_with_resumable_execution_context(
    tmp_path: Path,
) -> None:
    brain = _BrainStub()
    context = _ContextStub(brain)

    result = await _open(context, tmp_path).send({"question": "why?", "n": 1})

    assert result == _Reply(message="ok")
    assert context.requested == ["functions/demo"]
    assert json.loads(brain.messages[0]) == {"question": "why?", "n": 1}
    state = brain.kwargs[0]["session_state"]["agent_execution_context"]
    assert state == {
        "run_id": "trace-1",
        "work_kind": "demo_kind",
        "work_identity": "conv-1",
        "resume_policy": "auto",
        "workspace_data_root": str(tmp_path),
        "read_only": False,
    }


@pytest.mark.asyncio
async def test_session_runs_in_a_created_directory_under_the_data_root(
    tmp_path: Path,
) -> None:
    brain = _BrainStub()
    cwd = tmp_path / "demo"
    assert not cwd.exists()

    await _open(_ContextStub(brain), tmp_path).send({})

    assert cwd.is_dir()
    assert brain.kwargs[0]["cwd"] == cwd


@pytest.mark.asyncio
async def test_repeated_sends_reuse_the_same_conversation_kwargs(
    tmp_path: Path,
) -> None:
    brain = _BrainStub()
    session = _open(_ContextStub(brain), tmp_path)

    await session.send({"turn": 1})
    await session.send({"turn": 2})

    assert [json.loads(message) for message in brain.messages] == [
        {"turn": 1},
        {"turn": 2},
    ]
    assert brain.kwargs[0] == brain.kwargs[1]


@pytest.mark.asyncio
async def test_unstructured_output_is_rejected_as_a_command_error(
    tmp_path: Path,
) -> None:
    session = _open(_ContextStub(_BrainStub("not structured")), tmp_path)

    with pytest.raises(AssistantResponseError) as caught:
        await session.send({})

    # App API maps assistant failures through the shared command-error path.
    assert isinstance(caught.value, CommandError)
    assert caught.value.prompt == "functions/demo"
    assert "functions/demo" in str(caught.value)


@pytest.mark.asyncio
async def test_turns_are_writable_unless_declared_read_only(tmp_path: Path) -> None:
    brain = _BrainStub()

    await _open(_ContextStub(brain), tmp_path).send({})

    state = brain.kwargs[0]["session_state"]["agent_execution_context"]
    assert state["read_only"] is False


@pytest.mark.asyncio
async def test_read_only_is_declared_to_the_agent_runtime(tmp_path: Path) -> None:
    brain = _BrainStub()
    session = open_assistant_session(
        _ContextStub(brain),
        prompt="functions/demo",
        work_kind="demo_kind",
        conversation_id="conv-1",
        trace_id="trace-1",
        result_type=_Reply,
        workspace_data_root=tmp_path,
        cwd_name="demo",
        read_only=True,
    )

    await session.send({})

    # The prompt describes the limit; the runtime is what enforces it.
    state = brain.kwargs[0]["session_state"]["agent_execution_context"]
    assert state["read_only"] is True
