"""The chat decision replay script runs a saved case set through the member's
configured brain and reports each case's selection."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from guildbotics.intelligences.brains import factory
from guildbotics.intelligences.decisions.chat_policy import QUESTIONS
from guildbotics.runtime.brain import Brain
from guildbotics.utils.fileio import GUILDBOTICS_CONFIG_DIR, GUILDBOTICS_WORKSPACE_ROOT
from tests.scripts.conftest import SCRIPTS_DIR


class ReplayBrain(Brain):
    """Answers every question with ``unknown``, recording who asked."""

    calls: list[str] = []

    async def run(self, message: str, **kwargs):
        ReplayBrain.calls.append(self.person_id)
        answers = {
            key: {"type": question.type, "value": "unknown"}
            for key, question in QUESTIONS.items()
        }
        return json.dumps({"answers": answers})


def _script():
    spec = importlib.util.spec_from_file_location(
        "evaluate_chat_decision", SCRIPTS_DIR / "evaluate-chat-decision.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_a_case_set_is_judged_by_the_members_configured_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / ".guildbotics" / "config").mkdir(parents=True)
    # Selecting the workspace sets these; setting them first restores them.
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace))
    monkeypatch.setenv(GUILDBOTICS_CONFIG_DIR, str(workspace / ".guildbotics/config"))
    monkeypatch.setitem(
        factory.person_brain_mapping,
        "alice",
        {"chat_decision": factory.BrainConfig(type=ReplayBrain)},
    )
    monkeypatch.setattr(ReplayBrain, "calls", [])
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "state": {"thread_context_complete": True},
                "cases": [{"id": "c1", "state": {}, "expected_routes": ["agent"]}],
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate-chat-decision.py",
            str(cases),
            "--workspace",
            str(workspace),
            "--person",
            "alice",
            "--report",
            str(report),
        ],
    )

    await _script().main()

    assert ReplayBrain.calls == ["alice"]
    [item] = json.loads(report.read_text(encoding="utf-8"))
    assert item["case"] == "c1"
    assert item["selection"]["route"] == "agent"
    assert item["matches_expectation"] is True
    assert item["config"]["brain"] == "chat_decision"
