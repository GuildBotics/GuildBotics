import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

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


def _model_config(**overrides) -> "agno_agent.ModelConfig":
    return agno_agent.ModelConfig(
        name="models/test.yml",
        model_class="tests.FakeModel",
        parameters={"id": "base-model", "temperature": 0.2},
        **overrides,
    )


async def _run_with_effort(
    monkeypatch,
    *,
    model_config,
    frontmatter_effort: str = "",
    session_state: dict | None = None,
) -> tuple[dict, list[tuple[str, dict]]]:
    """Run the brain and return the model parameters it instantiated."""
    original = agno_agent.person_model_mapping.copy()
    io_records: list[tuple[str, dict]] = []
    captured: dict = {}
    agno_agent.person_model_mapping.clear()
    agno_agent.person_model_mapping["p1"] = {"default": model_config}

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def arun(self, message: str):
            return type("R", (), {"content": "reply"})()

    def fake_instantiate(*args, **kwargs):
        kwargs.pop("expected_type", None)
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        agno_agent,
        "record_correlated_io",
        lambda *, io_type, payload: io_records.append((io_type, payload)),
    )
    monkeypatch.setattr(agno_agent, "record_span_summary", lambda **kwargs: None)
    monkeypatch.setattr(agno_agent, "instantiate_class", fake_instantiate)
    monkeypatch.setattr(agno_agent, "Agent", FakeAgent)

    try:
        brain = agno_agent.AgnoAgentDefaultBrain(
            "p1",
            "functions/reply",
            logger=logging.getLogger("test"),
            effort=frontmatter_effort,
        )
        await brain.run("hello", session_state=session_state or {})
    finally:
        agno_agent.person_model_mapping.clear()
        agno_agent.person_model_mapping.update(original)
    return captured, io_records


@pytest.mark.asyncio
async def test_effort_overlay_merges_into_model_parameters(monkeypatch) -> None:
    config = _model_config(effort={"high": {"reasoning_effort": "high"}})
    parameters, _ = await _run_with_effort(
        monkeypatch, model_config=config, frontmatter_effort="high"
    )
    assert parameters == {
        "id": "base-model",
        "temperature": 0.2,
        "reasoning_effort": "high",
    }


@pytest.mark.asyncio
async def test_effort_overlay_may_replace_the_model_id(monkeypatch) -> None:
    config = _model_config(effort={"high": {"id": "stronger-model"}})
    parameters, io_records = await _run_with_effort(
        monkeypatch, model_config=config, frontmatter_effort="high"
    )
    assert parameters["id"] == "stronger-model"
    assert io_records[0][1]["effort"]["model"] == "stronger-model"


@pytest.mark.asyncio
async def test_effort_overlay_preserves_nested_and_numeric_values(monkeypatch) -> None:
    config = _model_config(
        effort={"high": {"thinking": {"type": "enabled", "budget_tokens": 8000}}}
    )
    parameters, _ = await _run_with_effort(
        monkeypatch, model_config=config, frontmatter_effort="high"
    )
    assert parameters["thinking"] == {"type": "enabled", "budget_tokens": 8000}


@pytest.mark.asyncio
async def test_runtime_effort_overrides_frontmatter(monkeypatch) -> None:
    config = _model_config(
        effort={
            "high": {"reasoning_effort": "high"},
            "low": {"reasoning_effort": "low"},
        }
    )
    parameters, io_records = await _run_with_effort(
        monkeypatch,
        model_config=config,
        frontmatter_effort="high",
        session_state={"effort": "low"},
    )
    assert parameters["reasoning_effort"] == "low"
    assert io_records[0][1]["effort"]["source"] == "runtime"


@pytest.mark.asyncio
async def test_runtime_default_cancels_a_frontmatter_high(monkeypatch) -> None:
    config = _model_config(effort={"high": {"reasoning_effort": "high"}})
    parameters, io_records = await _run_with_effort(
        monkeypatch,
        model_config=config,
        frontmatter_effort="high",
        session_state={"effort": "default"},
    )
    assert "reasoning_effort" not in parameters
    assert io_records[0][1]["effort"]["resolved"] == "default"


@pytest.mark.asyncio
async def test_undefined_level_continues_without_intervention(
    monkeypatch, caplog
) -> None:
    config = _model_config(effort={})
    with caplog.at_level(logging.WARNING):
        parameters, io_records = await _run_with_effort(
            monkeypatch, model_config=config, frontmatter_effort="high"
        )
    assert parameters == {"id": "base-model", "temperature": 0.2}
    assert io_records[0][1]["effort"]["unsupported"] is True
    assert "high" in caplog.text


@pytest.mark.asyncio
async def test_request_diagnostics_never_carry_effort_setting_values(
    monkeypatch,
) -> None:
    config = _model_config(effort={"high": {"api_key": "sk-secret"}})
    _, io_records = await _run_with_effort(
        monkeypatch, model_config=config, frontmatter_effort="high"
    )
    effort_payload = io_records[0][1]["effort"]
    assert effort_payload["applied_keys"] == ["api_key"]
    assert "sk-secret" not in str(effort_payload)


def test_model_config_rejects_an_unknown_effort_level() -> None:
    with pytest.raises(ValidationError):
        agno_agent.ModelConfig(
            name="models/test.yml",
            model_class="tests.FakeModel",
            effort={"extreme": {"reasoning_effort": "high"}},
        )


def test_a_slot_inherits_its_providers_effort_when_it_states_none(
    monkeypatch, tmp_path
) -> None:
    """Only `default.yml` is packaged, so a second slot would otherwise be bare.

    The editor shows this slot as inheriting; the runtime has to agree, or the
    screen would promise an effort mapping that never gets applied.
    """
    config = tmp_path / ".guildbotics/config/intelligences"
    (config / "models/openai").mkdir(parents=True)
    (config / "model_mapping.yml").write_text(
        "writer: models/openai/writer.yml\n", encoding="utf-8"
    )
    (config / "models/openai/default.yml").write_text(
        "model_class: agno.models.openai.OpenAIChat\n"
        "parameters:\n  id: gpt-default\n"
        "effort:\n  high:\n    reasoning_effort: high\n",
        encoding="utf-8",
    )
    (config / "models/openai/writer.yml").write_text(
        "model_class: agno.models.openai.OpenAIChat\nparameters:\n  id: gpt-writer\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))
    agno_agent.person_model_mapping.clear()
    try:
        mapping = agno_agent.get_model_mapping("alice")
    finally:
        agno_agent.person_model_mapping.clear()

    assert mapping["writer"].effort == {"high": {"reasoning_effort": "high"}}


def test_an_explicit_empty_effort_is_not_refilled_by_the_provider(
    monkeypatch, tmp_path
) -> None:
    """`effort: {}` means "none"; only an absent key inherits."""
    config = tmp_path / ".guildbotics/config/intelligences"
    (config / "models/openai").mkdir(parents=True)
    (config / "model_mapping.yml").write_text(
        "writer: models/openai/writer.yml\n", encoding="utf-8"
    )
    (config / "models/openai/default.yml").write_text(
        "model_class: agno.models.openai.OpenAIChat\n"
        "parameters:\n  id: gpt-default\n"
        "effort:\n  high:\n    reasoning_effort: high\n",
        encoding="utf-8",
    )
    (config / "models/openai/writer.yml").write_text(
        "model_class: agno.models.openai.OpenAIChat\n"
        "parameters:\n  id: gpt-writer\n"
        "effort: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))
    agno_agent.person_model_mapping.clear()
    try:
        mapping = agno_agent.get_model_mapping("alice")
    finally:
        agno_agent.person_model_mapping.clear()

    assert mapping["writer"].effort == {}


def test_a_definition_saved_before_effort_existed_still_inherits(
    monkeypatch, tmp_path
) -> None:
    """A workspace file shadows the packaged one wholesale.

    A `models/<provider>/default.yml` written before effort existed states no
    `effort:`, so without the packaged fallback the provider's mapping would be
    lost for good and the setting screen would promise one that never applies.
    """
    config = tmp_path / ".guildbotics/config/intelligences"
    (config / "models/openai").mkdir(parents=True)
    (config / "model_mapping.yml").write_text(
        "default: models/openai/default.yml\n", encoding="utf-8"
    )
    (config / "models/openai/default.yml").write_text(
        "model_class: agno.models.openai.OpenAIChat\nparameters:\n  id: gpt-old\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path / ".guildbotics/config"))
    agno_agent.person_model_mapping.clear()
    try:
        mapping = agno_agent.get_model_mapping("alice")
    finally:
        agno_agent.person_model_mapping.clear()

    assert mapping["default"].effort == {
        "low": {"reasoning_effort": "low"},
        "high": {"reasoning_effort": "high"},
    }
    # The workspace's own model id still wins; only the absent key inherits.
    assert mapping["default"].parameters["id"] == "gpt-old"
