"""Replay uses the resolved member brain and never reaches chat operations."""

import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest

from guildbotics.intelligences.decisions.chat_policy import QUESTIONS
from guildbotics.runtime.brain import ExecutionMetadata
from tests.scripts.conftest import SCRIPTS_DIR


@pytest.mark.asyncio
async def test_a_case_set_is_judged_by_the_members_configured_brain(
    tmp_path, monkeypatch
):
    spec = importlib.util.spec_from_file_location(
        "decision_replay", SCRIPTS_DIR / "evaluate-chat-decision.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    records = []

    class Brain:
        configuration = {"model": "configured-member-model"}
        execution = ExecutionMetadata()
        probabilistic_answers = False

        async def run(self, message):
            assert [payload.get("phase") for _, payload in records] == [
                "started",
                "resolved",
            ]
            return {
                "answers": {
                    key: {"type": q.type, "value": "unknown"}
                    for key, q in QUESTIONS.items()
                }
            }

    def create(person, *args, **kwargs):
        calls.append((person, kwargs))
        return Brain()

    recorder = SimpleNamespace(
        io=lambda key, value: records.append((key, value)), evaluated=lambda *args: None
    )
    monkeypatch.setattr(
        module,
        "replay_context",
        lambda person, workspace: (SimpleNamespace(create_brain=create), recorder),
    )
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "state": {"thread_context_complete": True},
                "cases": [{"id": "c1", "state": {}, "expected_routes": ["agent"]}],
            }
        )
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate-chat-decision.py",
            str(cases),
            "--workspace",
            str(tmp_path),
            "--person",
            "alice",
            "--report",
            str(report),
        ],
    )
    await module.main()
    assert calls[0][0] == "alice"
    assert calls[0][1]["config"]["brain"] == "chat_decision"
    [item] = json.loads(report.read_text())
    assert item["case"] == "c1"
    assert item["selection"]["route"] == "agent"
    assert item["matches_expectation"] is True
    assert item["config"]["brain"] == "chat_decision"
    assert len(records) == 3
    assert "input" not in item and "result" not in item
