import inspect
import logging
from copy import deepcopy
from pathlib import Path

import pytest
from agno.agent import Agent
from pydantic import BaseModel, ValidationError

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
            parameters={"id": "test-model-5"},
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
            logger=logging.getLogger("test"),
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
    # The span names the model the request was really made with, and keeps the
    # slot definition name on an attribute so traces stay searchable by slot.
    assert span_records[0]["model"] == "test-model-5"
    assert span_records[0]["effort"] == ""
    assert span_records[0]["attributes"] == {"model.slot": "models/test.yml"}


@pytest.mark.asyncio
async def test_agent_kwargs_are_accepted_by_the_installed_agno(monkeypatch) -> None:
    """Every other test fakes ``Agent``, so a renamed agno parameter slips through.

    The brain builds the kwargs by name, so binding them against the real
    signature is what catches an upgrade that renames one of them.
    """

    class Reply(BaseModel):
        text: str = ""

    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def arun(self, message: str):
            return type("R", (), {"content": "reply"})()

    original = agno_agent.person_model_mapping.copy()
    agno_agent.person_model_mapping.clear()
    agno_agent.person_model_mapping["p1"] = {"default": _model_config()}
    monkeypatch.setattr(agno_agent, "record_correlated_io", lambda **kwargs: None)
    monkeypatch.setattr(agno_agent, "record_span_summary", lambda **kwargs: None)
    monkeypatch.setattr(
        agno_agent, "instantiate_class", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(agno_agent, "Agent", FakeAgent)

    try:
        brain = agno_agent.AgnoAgentDefaultBrain(
            "p1",
            "functions/reply",
            logger=logging.getLogger("test"),
            description="System prompt",
            response_class=Reply,
        )
        await brain.run("hello", session_state={"topic": "style"})
    finally:
        agno_agent.person_model_mapping.clear()
        agno_agent.person_model_mapping.update(original)

    # The structured-output model reaches agno under its own parameter name.
    assert captured["output_schema"] is Reply
    assert "response_model" not in captured
    inspect.signature(Agent.__init__).bind_partial(None, **captured)


@pytest.mark.asyncio
async def test_the_runtime_context_never_reaches_the_agent(monkeypatch) -> None:
    """agno deep-copies the session state on every run, so it must be copyable.

    ``functions.to_dict`` puts the live ``Context`` into the session state for
    GuildBotics' own placeholder substitution. That handle owns open HTTP
    clients, which ``deepcopy`` cannot copy, and its repr is meaningless to a
    model — so the brain has to keep it out of what it hands to agno.
    """

    class Uncopyable:
        """Stands in for a Context holding a live client."""

        def __deepcopy__(self, memo):
            raise TypeError("cannot pickle '_thread.RLock' object")

    captured: dict = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def arun(self, message: str):
            return type("R", (), {"content": "reply"})()

    original = agno_agent.person_model_mapping.copy()
    agno_agent.person_model_mapping.clear()
    agno_agent.person_model_mapping["p1"] = {"default": _model_config()}
    monkeypatch.setattr(agno_agent, "record_correlated_io", lambda **kwargs: None)
    monkeypatch.setattr(agno_agent, "record_span_summary", lambda **kwargs: None)
    monkeypatch.setattr(
        agno_agent, "instantiate_class", lambda *args, **kwargs: object()
    )
    monkeypatch.setattr(agno_agent, "Agent", FakeAgent)

    try:
        brain = agno_agent.AgnoAgentDefaultBrain(
            "p1", "functions/reply", logger=logging.getLogger("test")
        )
        await brain.run(
            "hello", session_state={"context": Uncopyable(), "topic": "style"}
        )
    finally:
        agno_agent.person_model_mapping.clear()
        agno_agent.person_model_mapping.update(original)

    assert captured["session_state"] == {"topic": "style"}
    # What agno receives has to survive the copy it makes on every run.
    deepcopy(captured["session_state"])
    # The dedicated dump-the-state-into-the-prompt option is not agno 1.x's
    # placeholder substitution; `resolve_in_context` (on by default) is.
    assert "add_session_state_to_context" not in captured


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
    span_records: list[dict] | None = None,
) -> tuple[dict, list[tuple[str, dict]]]:
    """Run the brain and return the model parameters it instantiated.

    Callers that care about the span summary pass ``span_records`` to collect
    the arguments every ``record_span_summary`` call was made with.
    """
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
    spans = span_records if span_records is not None else []
    monkeypatch.setattr(
        agno_agent, "record_span_summary", lambda **kwargs: spans.append(kwargs)
    )
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
async def test_span_summary_reports_the_model_the_overlay_switched_to(
    monkeypatch,
) -> None:
    config = _model_config(effort={"high": {"id": "stronger-model"}})
    spans: list[dict] = []
    await _run_with_effort(
        monkeypatch,
        model_config=config,
        frontmatter_effort="high",
        span_records=spans,
    )
    assert spans[0]["model"] == "stronger-model"
    assert spans[0]["effort"] == "high"


@pytest.mark.asyncio
async def test_span_summary_reports_no_effort_when_the_level_is_unmapped(
    monkeypatch,
) -> None:
    """An unmapped level changes nothing, so the span must not claim it ran."""
    spans: list[dict] = []
    await _run_with_effort(
        monkeypatch,
        model_config=_model_config(effort={}),
        frontmatter_effort="high",
        span_records=spans,
    )
    assert spans[0]["model"] == "base-model"
    assert spans[0]["effort"] == ""


@pytest.mark.asyncio
async def test_span_model_stays_empty_when_the_definition_names_no_id(
    monkeypatch,
) -> None:
    """A request without an id runs on the provider default, which is unknown.

    The slot name must not stand in for it; the span stays attributable through
    ``model.slot``."""
    spans: list[dict] = []
    config = agno_agent.ModelConfig(
        name="models/test.yml",
        model_class="tests.FakeModel",
        parameters={"temperature": 0.2},
    )
    await _run_with_effort(monkeypatch, model_config=config, span_records=spans)
    assert spans[0]["model"] == ""
    assert spans[0]["attributes"] == {"model.slot": "models/test.yml"}


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
