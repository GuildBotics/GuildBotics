import json
from pathlib import Path

import pytest

from guildbotics.intelligences.brains import agno_agent


@pytest.mark.asyncio
async def test_agno_agent_prompt_trace_records_request_and_response(
    monkeypatch, tmp_path: Path
) -> None:
    original = agno_agent.person_model_mapping.copy()
    trace_path = tmp_path / "prompt_trace.jsonl"
    agno_agent.person_model_mapping.clear()
    agno_agent.person_model_mapping["p1"] = {
        "default": agno_agent.ModelConfig(
            name="models/test.yml",
            model_class="tests.FakeModel",
            parameters={},
        )
    }

    class FakeResponse:
        content = "reply"

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def arun(self, message: str):
            assert message == "hello"
            return FakeResponse()

    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE", "1")
    monkeypatch.setenv("GUILDBOTICS_PROMPT_TRACE_PATH", str(trace_path))
    monkeypatch.setattr(
        agno_agent, "instantiate_class", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(agno_agent, "Agent", FakeAgent)

    try:
        brain = agno_agent.AgnoAgentDefaultBrain(
            "p1",
            "functions/reply",
            logger=type("L", (), {})(),
            description="System prompt",
        )
        output = await brain.run("hello", session_state={"topic": "style"})
    finally:
        agno_agent.person_model_mapping.clear()
        agno_agent.person_model_mapping.update(original)

    assert output == "reply"
    events = [
        json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event"] for event in events] == ["llm.request", "llm.response"]
    assert events[0]["person_id"] == "p1"
    assert events[0]["brain"] == "functions/reply"
    assert events[0]["model"] == "models/test.yml"
    assert events[0]["description"] == "System prompt"
    assert events[0]["message"] == "hello"
    assert events[0]["session_state"] == {"topic": "style"}
    assert events[1]["content"] == "reply"
