from __future__ import annotations

from pathlib import Path

import pytest

from guildbotics.app_api import intelligences as intelligences_module
from guildbotics.app_api.intelligences import (
    AGNO_BRAIN_CLASS,
    CLI_BRAIN_CLASS,
    IntelligenceConfigService,
)
from guildbotics.app_api.models import (
    BrainAssignment,
    CliAgentDefinition,
    IntelligenceConfigUpdateRequest,
    ModelDefinition,
)
from guildbotics.editions.simple import simple_brain_factory
from guildbotics.intelligences.brains import agno_agent, cli_agent
from guildbotics.utils.fileio import load_yaml_file, save_yaml_file


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(path, data)


def _team_intelligences(config_dir: Path) -> Path:
    return config_dir / "intelligences"


def _member_intelligences(config_dir: Path, person_id: str) -> Path:
    return config_dir / "team/members" / person_id / "intelligences"


def _write_team_config(config_dir: Path) -> None:
    """Write a minimal but complete team-scoped intelligences config."""
    base = _team_intelligences(config_dir)
    _write_yaml(
        base / "model_mapping.yml",
        {
            "default": "models/openai/gpt.yml",
            "openai": "models/openai/gpt.yml",
        },
    )
    _write_yaml(
        base / "models/openai/gpt.yml",
        {"model_class": "team.ModelClass", "parameters": {"id": "team-model-id"}},
    )
    _write_yaml(
        base / "cli_agent_mapping.yml",
        {"default": "codex-cli.yml"},
    )
    _write_yaml(
        base / "cli_agents/codex-cli.yml",
        {"env": {"FOO": "bar"}, "script": "codex exec"},
    )
    _write_yaml(
        base / "brain_mapping.yml",
        {
            "default": {
                "class": AGNO_BRAIN_CLASS,
                "args": {"model": "default"},
            },
            "file_editor": {
                "class": CLI_BRAIN_CLASS,
                "args": {"cli_agent": "default"},
            },
        },
    )


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    simple_brain_factory.person_brain_mapping.clear()
    agno_agent.person_model_mapping.clear()
    cli_agent.person_cli_agent_mapping.clear()


# --------------------------------------------------------------------------- #
# read_config
# --------------------------------------------------------------------------- #


def test_read_config_template_fallback_when_team_config_absent(tmp_path: Path) -> None:
    """No team config on disk -> falls back to the packaged template mappings."""
    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert response.person_id is None
    assert response.inherited is False
    # Template ships a "default" model mapping entry.
    assert "default" in response.model_mapping
    assert response.models, "template models should be read via fallback"
    assert all(model.path.startswith("models/") for model in response.models)
    assert response.brain_mapping, "template brain mapping should be parsed"


def test_read_config_member_without_override_is_inherited(tmp_path: Path) -> None:
    _write_team_config(tmp_path)

    response = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )

    assert response.person_id == "alice"
    assert response.inherited is True
    # Inherited values come from the team scope.
    assert response.model_mapping["default"] == "models/openai/gpt.yml"
    assert response.models[0].model_id == "team-model-id"


def test_read_config_member_with_override_not_inherited(tmp_path: Path) -> None:
    _write_team_config(tmp_path)
    member_base = _member_intelligences(tmp_path, "alice")
    _write_yaml(
        member_base / "model_mapping.yml",
        {"default": "models/anthropic/claude.yml"},
    )
    _write_yaml(
        member_base / "models/anthropic/claude.yml",
        {"model_class": "member.ModelClass", "parameters": {"id": "member-model-id"}},
    )

    response = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )

    assert response.inherited is False
    assert response.model_mapping["default"] == "models/anthropic/claude.yml"
    assert response.models[0].provider == "anthropic"
    assert response.models[0].model_id == "member-model-id"


def test_read_config_deduplicates_model_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model path referenced by several mapping keys is read only once."""
    base = _team_intelligences(tmp_path)
    _write_yaml(
        base / "model_mapping.yml",
        {
            "default": "models/openai/gpt.yml",
            "openai": "models/openai/gpt.yml",
            "fast": "models/openai/gpt.yml",
        },
    )
    _write_yaml(
        base / "models/openai/gpt.yml",
        {"model_class": "team.ModelClass", "parameters": {"id": "team-model-id"}},
    )
    _write_yaml(base / "cli_agent_mapping.yml", {})
    _write_yaml(base / "brain_mapping.yml", {})

    model_file = (base / "models/openai/gpt.yml").resolve()
    read_counts: dict[Path, int] = {}
    real_load = load_yaml_file

    def counting_load(file: Path):
        resolved = Path(file).resolve()
        read_counts[resolved] = read_counts.get(resolved, 0) + 1
        return real_load(file)

    monkeypatch.setattr(intelligences_module, "load_yaml_file", counting_load)

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert len(response.models) == 1
    assert read_counts.get(model_file) == 1


def test_read_config_handles_malformed_yaml(tmp_path: Path) -> None:
    """Malformed model file -> empty model fields, no crash."""
    base = _team_intelligences(tmp_path)
    _write_yaml(base / "model_mapping.yml", {"default": "models/openai/gpt.yml"})
    (base / "models/openai").mkdir(parents=True, exist_ok=True)
    # A YAML scalar (string), not a mapping -> treated as empty dict.
    (base / "models/openai/gpt.yml").write_text("just-a-string\n", encoding="utf-8")
    _write_yaml(base / "cli_agent_mapping.yml", {})
    _write_yaml(base / "brain_mapping.yml", {})

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert len(response.models) == 1
    model = response.models[0]
    assert model.path == "models/openai/gpt.yml"
    assert model.model_class == ""
    assert model.model_id == ""


def test_read_config_cli_agent_env_not_dict_falls_back(tmp_path: Path) -> None:
    base = _team_intelligences(tmp_path)
    _write_yaml(base / "model_mapping.yml", {})
    _write_yaml(base / "cli_agent_mapping.yml", {"default": "codex-cli.yml"})
    # env is a list rather than a dict.
    (base / "cli_agents").mkdir(parents=True, exist_ok=True)
    (base / "cli_agents/codex-cli.yml").write_text(
        "env:\n  - not-a-dict\nscript: run\n", encoding="utf-8"
    )
    _write_yaml(base / "brain_mapping.yml", {})

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert len(response.cli_agents) == 1
    agent = response.cli_agents[0]
    assert agent.env == {}
    assert agent.script == "run"
    assert agent.name == "codex-cli"


def test_read_config_cli_agent_detected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _team_intelligences(tmp_path)
    _write_yaml(base / "model_mapping.yml", {})
    _write_yaml(base / "cli_agent_mapping.yml", {"default": "codex-cli.yml"})
    _write_yaml(base / "cli_agents/codex-cli.yml", {"env": {}, "script": "codex run"})
    _write_yaml(base / "brain_mapping.yml", {})

    def fake_resolve_cli_agent_path(executable: str) -> str:
        # The service strips the "-cli" suffix before resolving.
        return "/usr/local/bin/codex" if executable == "codex" else ""

    monkeypatch.setattr(
        intelligences_module, "resolve_cli_agent_path", fake_resolve_cli_agent_path
    )

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    agent = response.cli_agents[0]
    assert agent.detected is True
    assert agent.detected_path == "/usr/local/bin/codex"


def test_read_config_brain_mapping_engine_classification(tmp_path: Path) -> None:
    base = _team_intelligences(tmp_path)
    _write_yaml(base / "model_mapping.yml", {})
    _write_yaml(base / "cli_agent_mapping.yml", {})
    _write_yaml(
        base / "brain_mapping.yml",
        {
            "llm_brain": {"class": AGNO_BRAIN_CLASS, "args": {"model": "openai"}},
            "cli_brain": {"class": CLI_BRAIN_CLASS, "args": {"cli_agent": "codex"}},
            "skipped": "not-a-dict",
        },
    )

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    by_name = {b.name: b for b in response.brain_mapping}
    assert set(by_name) == {"llm_brain", "cli_brain"}
    assert by_name["llm_brain"].engine == "llm"
    assert by_name["llm_brain"].target == "openai"
    assert by_name["cli_brain"].engine == "cli"
    assert by_name["cli_brain"].target == "codex"


# --------------------------------------------------------------------------- #
# update_config (team scope)
# --------------------------------------------------------------------------- #


def _team_update_request(config_dir: Path) -> IntelligenceConfigUpdateRequest:
    return IntelligenceConfigUpdateRequest(
        config_dir=config_dir,
        person_id=None,
        model_mapping={"default": "models/openai/gpt.yml"},
        models=[
            ModelDefinition(
                path="models/openai/gpt.yml",
                provider="openai",
                model_class="openai.Class",
                model_id="gpt-test",
            )
        ],
        cli_agent_mapping={"default": "codex-cli.yml"},
        cli_agents=[
            CliAgentDefinition(
                path="codex-cli.yml",
                name="codex-cli",
                env={"KEY": "value"},
                script="codex exec",
            )
        ],
        brain_mapping=[
            BrainAssignment(
                name="default",
                brain_class=AGNO_BRAIN_CLASS,
                engine="llm",
                target="default",
            ),
            BrainAssignment(
                name="file_editor",
                brain_class=CLI_BRAIN_CLASS,
                engine="cli",
                target="codex",
            ),
        ],
    )


def test_team_update_writes_all_files(tmp_path: Path) -> None:
    request = _team_update_request(tmp_path)

    result = IntelligenceConfigService().update_config(request)

    base = _team_intelligences(tmp_path)
    model_file = base / "models/openai/gpt.yml"
    cli_file = base / "cli_agents/codex-cli.yml"

    written = {f.path for f in result.files}
    assert (base / "model_mapping.yml") in written
    assert model_file in written
    assert (base / "cli_agent_mapping.yml") in written
    assert cli_file in written
    assert (base / "brain_mapping.yml") in written
    assert all(f.action == "update" for f in result.files)

    # Files exist on disk with expected content.
    assert load_yaml_file(base / "model_mapping.yml") == {
        "default": "models/openai/gpt.yml"
    }
    model_data = load_yaml_file(model_file)
    assert model_data["model_class"] == "openai.Class"
    assert model_data["parameters"]["id"] == "gpt-test"
    cli_data = load_yaml_file(cli_file)
    assert cli_data["env"] == {"KEY": "value"}
    assert cli_data["script"] == "codex exec"

    brain_data = load_yaml_file(base / "brain_mapping.yml")
    assert brain_data["default"] == {
        "class": AGNO_BRAIN_CLASS,
        "args": {"model": "default"},
    }
    assert brain_data["file_editor"] == {
        "class": CLI_BRAIN_CLASS,
        "args": {"cli_agent": "codex"},
    }


def test_team_update_merges_existing_model_file(tmp_path: Path) -> None:
    """Existing model file extra parameters are preserved on update."""
    base = _team_intelligences(tmp_path)
    temperature = 0.5
    _write_yaml(
        base / "models/openai/gpt.yml",
        {
            "model_class": "old.Class",
            "parameters": {"id": "old-id", "temperature": temperature},
        },
    )

    request = _team_update_request(tmp_path)
    IntelligenceConfigService().update_config(request)

    model_data = load_yaml_file(base / "models/openai/gpt.yml")
    assert model_data["model_class"] == "openai.Class"
    assert model_data["parameters"]["id"] == "gpt-test"
    assert model_data["parameters"]["temperature"] == temperature


def test_team_update_clears_all_runtime_caches(tmp_path: Path) -> None:
    simple_brain_factory.person_brain_mapping["alice"] = {}
    agno_agent.person_model_mapping["alice"] = {}
    cli_agent.person_cli_agent_mapping["bob"] = {}

    IntelligenceConfigService().update_config(_team_update_request(tmp_path))

    assert simple_brain_factory.person_brain_mapping == {}
    assert agno_agent.person_model_mapping == {}
    assert cli_agent.person_cli_agent_mapping == {}


# --------------------------------------------------------------------------- #
# update_config (member scope)
# --------------------------------------------------------------------------- #


def test_member_override_update_writes_only_member_mapping_files(
    tmp_path: Path,
) -> None:
    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={"default": "models/openai/gpt.yml"},
        cli_agent_mapping={"default": "codex-cli.yml"},
    )

    result = IntelligenceConfigService().update_config(request)

    base = _member_intelligences(tmp_path, "alice")
    written = {f.path for f in result.files}
    assert written == {
        base / "model_mapping.yml",
        base / "cli_agent_mapping.yml",
    }
    # Member override does NOT write model files, cli_agents, or brain mapping.
    assert not (base / "brain_mapping.yml").exists()
    assert not (base / "cli_agents").exists()
    assert not (base / "models").exists()
    assert load_yaml_file(base / "model_mapping.yml") == {
        "default": "models/openai/gpt.yml"
    }


def test_member_override_update_replaces_existing_dir(tmp_path: Path) -> None:
    base = _member_intelligences(tmp_path, "alice")
    stale = base / "stale.yml"
    _write_yaml(stale, {"old": "data"})

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={"default": "models/openai/gpt.yml"},
        cli_agent_mapping={},
    )
    IntelligenceConfigService().update_config(request)

    assert not stale.exists()
    assert (base / "model_mapping.yml").exists()


def test_member_override_update_clears_only_member_cache(tmp_path: Path) -> None:
    simple_brain_factory.person_brain_mapping["alice"] = {}
    simple_brain_factory.person_brain_mapping["bob"] = {}
    agno_agent.person_model_mapping["alice"] = {}
    cli_agent.person_cli_agent_mapping["alice"] = {}

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={},
        cli_agent_mapping={},
    )
    IntelligenceConfigService().update_config(request)

    assert "alice" not in simple_brain_factory.person_brain_mapping
    assert "bob" in simple_brain_factory.person_brain_mapping
    assert "alice" not in agno_agent.person_model_mapping
    assert "alice" not in cli_agent.person_cli_agent_mapping


def test_inherit_team_defaults_deletes_member_intelligences(tmp_path: Path) -> None:
    base = _member_intelligences(tmp_path, "alice")
    _write_yaml(base / "model_mapping.yml", {"default": "models/openai/gpt.yml"})
    simple_brain_factory.person_brain_mapping["alice"] = {}

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        inherit_team_defaults=True,
    )
    result = IntelligenceConfigService().update_config(request)

    assert not base.exists()
    assert len(result.files) == 1
    assert result.files[0].path == base
    assert result.files[0].action == "delete"
    assert "alice" not in simple_brain_factory.person_brain_mapping


def test_inherit_team_defaults_when_no_member_dir(tmp_path: Path) -> None:
    """Inherit with no existing member dir is a no-op delete that still reports."""
    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        inherit_team_defaults=True,
    )
    result = IntelligenceConfigService().update_config(request)

    base = _member_intelligences(tmp_path, "alice")
    assert not base.exists()
    assert len(result.files) == 1
    assert result.files[0].action == "delete"
