import pytest
from pydantic import ValidationError
from guildbotics.intelligences.brains import agno_agent


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
    mapping = agno_agent.get_model_mapping("alice")

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
    mapping = agno_agent.get_model_mapping("alice")

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
    mapping = agno_agent.get_model_mapping("alice")

    assert mapping["default"].effort == {
        "low": {"reasoning_effort": "low"},
        "high": {"reasoning_effort": "high"},
    }
    # The workspace's own model id still wins; only the absent key inherits.
    assert mapping["default"].parameters["id"] == "gpt-old"
