"""Replay evidence is a prerequisite for omitting an agent turn."""

import copy
import logging
from types import SimpleNamespace

import pytest

from guildbotics.intelligences.decisions.assessment import assess
from guildbotics.intelligences.decisions.chat_policy import QUESTIONS, QUESTION_VERSION
from guildbotics.intelligences.decisions.models import DecisionConfig
from guildbotics.runtime.brain import ExecutionMetadata


class Recorder:
    def __init__(self, fail=""):
        self.records = []
        self.events = []
        self.fail = fail

    def io(self, record_id, payload):
        if payload.get("phase", "result") == self.fail:
            raise OSError("unavailable recording store")
        self.records.append((record_id, copy.deepcopy(payload)))

    def evaluated(self, person_id, payload):
        self.events.append((person_id, payload))


class Brain:
    configuration = {"model": "test", "parameters": {"temperature": 0.2}}
    probabilistic_answers = False

    def __init__(self, recorder, *, failed=False, work=False):
        self.recorder = recorder
        self.failed = failed
        self.work = work
        self.calls = []
        self.execution = ExecutionMetadata()

    async def run(self, message):
        assert [p.get("phase") for _, p in self.recorder.records] == [
            "started",
            "resolved",
        ]
        self.calls.append(message)
        if self.failed:
            raise ValueError("untrusted provider message")
        values = dict.fromkeys(QUESTIONS, "false")
        values.update(context_sufficient="true", ack_only="true", reaction="ack")
        if self.work:
            values.update(work_files="true", pending_request="true")
        return {
            "answers": {
                key: {"type": QUESTIONS[key].type, "value": value}
                for key, value in values.items()
            }
        }


def factory(brain):
    calls = []

    def create(*args, **kwargs):
        calls.append((args, kwargs))
        return brain

    return SimpleNamespace(create_brain=create, calls=calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["started", "resolved", "result"])
async def test_each_required_record_failure_delegates_and_prior_failures_prevent_inference(
    fail,
):
    recorder = Recorder(fail)
    brain = Brain(recorder)
    selection, _ = await assess(
        {"thread_context_complete": True},
        None,
        person_id="aiko",
        logger=logging.getLogger(__name__),
        brain_factory=factory(brain),
        recorder=recorder,
    )
    assert selection.route == "agent"
    assert selection.reason == "invalid"
    assert bool(brain.calls) == (fail == "result")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed,work", [(False, False), (True, False), (False, True)])
async def test_full_snapshot_configuration_result_and_effort_survive_without_transcripts(
    failed, work
):
    recorder = Recorder()
    brain = Brain(recorder, failed=failed, work=work)
    brains = factory(brain)
    state = {
        "thread_context_complete": True,
        "unprocessed_messages": [{"content": "ありがとう" * 600}],
    }
    selection, evaluation_id = await assess(
        state,
        DecisionConfig(brain="custom"),
        person_id="aiko",
        logger=logging.getLogger(__name__),
        brain_factory=brains,
        recorder=recorder,
    )
    assert [key for key, _ in recorder.records] == [
        evaluation_id + "0",
        evaluation_id + "1",
        evaluation_id,
    ]
    saved = recorder.records[-1][1]
    assert saved["input"]["state"] == state
    assert saved["question_version"] == QUESTION_VERSION
    assert (
        saved["input"]["questions"]["reaction"]["criteria"]
        == QUESTIONS["reaction"].criteria
    )
    assert len(saved["input_hash"]) == 64
    assert recorder.records[1][1]["configuration"] == brain.configuration
    assert brains.calls[0][1]["config"]["brain"] == "custom"
    assert "untrusted provider message" not in str(saved)
    assert selection.route == ("agent" if failed or work else "reaction-only")
    assert selection.response_effort == ("high" if work else None)
    assert recorder.events[0][1]["response_effort"] == selection.response_effort


@pytest.mark.asyncio
async def test_incomplete_input_records_its_decision_and_never_calls_inference():
    recorder = Recorder()
    brain = Brain(recorder)
    selection, _ = await assess(
        {},
        None,
        person_id="aiko",
        logger=logging.getLogger(__name__),
        brain_factory=factory(brain),
        recorder=recorder,
    )
    assert selection.route == "agent"
    assert recorder.records[-1][1]["result"]["error"] == "incomplete_input"
    assert not brain.calls
