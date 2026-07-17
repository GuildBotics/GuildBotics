from pathlib import Path

import pytest

from guildbotics.intelligences.brains import agno_agent


@pytest.mark.asyncio
async def test_agno_agent_records_request_response_and_span(
    monkeypatch, tmp_path: Path
) -> None:
    original = agno_agent.person_model_mapping.copy()
    io_records: list[tuple[str, dict]] = []
    span_records: list[dict] = []
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

    monkeypatch.setattr(
        agno_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )
    monkeypatch.setattr(
        agno_agent,
        "record_span_summary",
        lambda **kwargs: span_records.append(kwargs),
    )
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
    assert [record[0] for record in io_records] == ["llm.request", "llm.response"]
    assert io_records[0][1]["person_id"] == "p1"
    assert io_records[0][1]["brain"] == "functions/reply"
    assert io_records[0][1]["model"] == "models/test.yml"
    assert io_records[0][1]["description"] == "System prompt"
    assert io_records[0][1]["message"] == "hello"
    assert io_records[0][1]["session_state"] == {"topic": "style"}
    assert io_records[1][1]["content"] == "reply"
    assert span_records[0].get("status", "finished") == "finished"
    assert span_records[0]["model"] == "models/test.yml"
