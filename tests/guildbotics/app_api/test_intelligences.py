from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from guildbotics.app_api import intelligences as intelligences_module
from guildbotics.app_api.intelligences import (
    AGNO_BRAIN_CLASS,
    CLI_BRAIN_CLASS,
    IntelligenceConfigService,
)
from guildbotics.app_api.models import (
    BrainAssignment,
    CliAgentDefinition,
    CodexNativeAgentPolicySettings,
    IntelligenceConfigUpdateRequest,
    ModelDefinition,
    NativeAgentPolicySettings,
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
            "agent": {
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
    assert response.native_agent_policy.codex.filesystem_access == "workspace"


def test_policy_update_model_rejects_coercion_and_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        IntelligenceConfigUpdateRequest(
            config_dir=tmp_path,
            native_agent_policy={
                "codex": {
                    "filesystem_access": "workspace",
                    "network_access": True,
                }
            },
        )


def test_read_config_member_policy_uses_member_then_team_scope(tmp_path: Path) -> None:
    _write_yaml(
        _team_intelligences(tmp_path) / "native_agent_policy.yml",
        {"codex": {"filesystem_access": "host"}},
    )

    inherited = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )
    assert inherited.native_agent_policy.codex.filesystem_access == "host"

    _write_yaml(
        _member_intelligences(tmp_path, "alice") / "native_agent_policy.yml",
        {"codex": {"filesystem_access": "workspace"}},
    )
    overridden = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )
    assert overridden.native_agent_policy.codex.filesystem_access == "workspace"


def test_read_config_member_without_override_is_inherited(tmp_path: Path) -> None:
    _write_team_config(tmp_path)

    response = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )

    assert response.person_id == "alice"
    assert response.inherited is True
    # Inherited values come from the team scope.
    assert response.model_mapping["default"] == "models/openai/gpt.yml"
    assert response.cli_agent_mapping["default"] == "codex"
    assert response.models[0].model_id == "team-model-id"


def test_read_config_member_marks_team_owned_slots(tmp_path: Path) -> None:
    """A member response flags the team-owned slot/feature names as locked."""
    _write_team_config(tmp_path)

    response = IntelligenceConfigService().read_config(
        config_dir=tmp_path, person_id="alice"
    )

    assert set(response.inherited_model_slots) == {"default", "openai"}
    assert set(response.inherited_cli_slots) == {"default"}
    assert set(response.inherited_brain_features) == {"default", "agent"}


def test_read_config_team_has_no_inherited_slots(tmp_path: Path) -> None:
    """The team scope owns everything, so nothing is reported as inherited."""
    _write_team_config(tmp_path)

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert response.inherited_model_slots == []
    assert response.inherited_cli_slots == []
    assert response.inherited_brain_features == []


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
    # A partial member override must not drop the team-provided slots: the
    # "openai" slot the member never redefined is still inherited from the team.
    assert response.model_mapping["openai"] == "models/openai/gpt.yml"
    member_model = next(m for m in response.models if m.provider == "anthropic")
    assert member_model.model_id == "member-model-id"


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
    _write_yaml(base / "cli_agent_mapping.yml", {"default": "custom-cli.yml"})
    # env is a list rather than a dict.
    (base / "cli_agents").mkdir(parents=True, exist_ok=True)
    (base / "cli_agents/custom-cli.yml").write_text(
        "env:\n  - not-a-dict\nscript: run\n", encoding="utf-8"
    )
    _write_yaml(base / "brain_mapping.yml", {})

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    assert len(response.cli_agents) == 1
    agent = response.cli_agents[0]
    assert agent.env == {}
    assert agent.script == "run"
    assert agent.name == "custom-cli"


def test_read_config_cli_agent_detected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _team_intelligences(tmp_path)
    _write_yaml(base / "model_mapping.yml", {})
    _write_yaml(base / "cli_agent_mapping.yml", {"default": "codex"})
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
            "agent": {"class": CLI_BRAIN_CLASS, "args": {"cli_agent": "codex"}},
            "skipped": "not-a-dict",
        },
    )

    response = IntelligenceConfigService().read_config(config_dir=tmp_path)

    by_name = {b.name: b for b in response.brain_mapping}
    assert set(by_name) == {"llm_brain", "agent"}
    assert by_name["llm_brain"].engine == "llm"
    assert by_name["llm_brain"].target == "openai"
    assert by_name["agent"].engine == "cli"
    assert by_name["agent"].target == "codex"


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
        cli_agent_mapping={"default": "codex"},
        cli_agents=[
            CliAgentDefinition(
                path="codex",
                name="codex",
                env={},
                script="",
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
                name="agent",
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
    cli_file = base / "cli_agents/codex"

    written = {f.path for f in result.files}
    assert (base / "model_mapping.yml") in written
    assert model_file in written
    assert (base / "cli_agent_mapping.yml") in written
    assert cli_file not in written
    assert (base / "brain_mapping.yml") in written
    assert all(f.action == "update" for f in result.files)

    # Files exist on disk with expected content.
    assert load_yaml_file(base / "model_mapping.yml") == {
        "default": "models/openai/gpt.yml"
    }
    assert load_yaml_file(base / "cli_agent_mapping.yml") == {"default": "codex"}
    model_data = load_yaml_file(model_file)
    assert model_data["model_class"] == "openai.Class"
    assert model_data["parameters"]["id"] == "gpt-test"
    assert not cli_file.exists()

    brain_data = load_yaml_file(base / "brain_mapping.yml")
    assert brain_data["default"] == {
        "class": AGNO_BRAIN_CLASS,
        "args": {"model": "default"},
    }
    assert brain_data["agent"] == {
        "class": CLI_BRAIN_CLASS,
        "args": {"cli_agent": "codex"},
    }


def test_team_update_writes_native_agent_policy(tmp_path: Path) -> None:
    request = _team_update_request(tmp_path).model_copy(
        update={
            "native_agent_policy": NativeAgentPolicySettings(
                codex=CodexNativeAgentPolicySettings(filesystem_access="host")
            )
        }
    )

    result = IntelligenceConfigService().update_config(request)

    policy_file = _team_intelligences(tmp_path) / "native_agent_policy.yml"
    assert policy_file in {item.path for item in result.files}
    assert load_yaml_file(policy_file) == {
        "codex": {"filesystem_access": "host"},
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


def _team_brain_assignments() -> list[BrainAssignment]:
    """The BrainAssignment list equivalent to the team config's brain_mapping."""
    return [
        BrainAssignment(
            name="default",
            brain_class=AGNO_BRAIN_CLASS,
            engine="llm",
            target="default",
        ),
        BrainAssignment(
            name="agent",
            brain_class=CLI_BRAIN_CLASS,
            engine="cli",
            target="default",
        ),
    ]


def test_member_override_writes_only_changed_slots(tmp_path: Path) -> None:
    """A member override persists only what differs from the team defaults.

    Slots, definitions, brain assignments, and the policy that match the team
    are left inherited (not re-written), so the runtime merge keeps serving the
    team value and later team changes still propagate.
    """
    _write_team_config(tmp_path)

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        # "default" changes to anthropic; "openai" matches the team default.
        model_mapping={
            "default": "models/anthropic/claude.yml",
            "openai": "models/openai/gpt.yml",
        },
        models=[
            ModelDefinition(
                path="models/anthropic/claude.yml",
                provider="anthropic",
                model_class="member.ModelClass",
                model_id="member-model-id",
            ),
            ModelDefinition(
                path="models/openai/gpt.yml",
                provider="openai",
                model_class="team.ModelClass",
                model_id="team-model-id",
            ),
        ],
        cli_agent_mapping={"default": "codex-cli.yml"},
        cli_agents=[
            CliAgentDefinition(
                path="codex-cli.yml",
                name="codex",
                env={"FOO": "bar"},
                script="codex exec",
            )
        ],
        brain_mapping=_team_brain_assignments(),
    )

    result = IntelligenceConfigService().update_config(request)

    base = _member_intelligences(tmp_path, "alice")
    written = {f.path for f in result.files}
    # Only the changed "default" model slot and its new model definition persist.
    assert written == {
        base / "model_mapping.yml",
        base / "models/anthropic/claude.yml",
    }
    assert load_yaml_file(base / "model_mapping.yml") == {
        "default": "models/anthropic/claude.yml"
    }
    # Slots/definitions/assignments identical to the team are left inherited.
    assert not (base / "models/openai").exists()
    assert not (base / "cli_agent_mapping.yml").exists()
    assert not (base / "cli_agents").exists()
    assert not (base / "brain_mapping.yml").exists()


def test_member_override_writes_changed_brain_assignment_only(tmp_path: Path) -> None:
    _write_team_config(tmp_path)
    assignments = _team_brain_assignments()
    # Add a member-only feature assignment; the inherited ones are unchanged.
    assignments.append(
        BrainAssignment(
            name="translate",
            brain_class=AGNO_BRAIN_CLASS,
            engine="llm",
            target="openai",
        )
    )

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={"default": "models/openai/gpt.yml", "openai": "models/openai/gpt.yml"},
        cli_agent_mapping={"default": "codex-cli.yml"},
        brain_mapping=assignments,
    )
    IntelligenceConfigService().update_config(request)

    base = _member_intelligences(tmp_path, "alice")
    stored = load_yaml_file(base / "brain_mapping.yml")
    # Only the added assignment is persisted; the team ones stay inherited.
    assert set(stored) == {"translate"}
    assert stored["translate"]["args"] == {"model": "openai"}


def test_member_override_update_writes_native_agent_policy(tmp_path: Path) -> None:
    policy = NativeAgentPolicySettings(
        codex=CodexNativeAgentPolicySettings(filesystem_access="host"),
    )
    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={},
        cli_agent_mapping={},
        native_agent_policy=policy,
    )

    IntelligenceConfigService().update_config(request)

    stored = load_yaml_file(
        _member_intelligences(tmp_path, "alice") / "native_agent_policy.yml"
    )
    assert stored == {"codex": {"filesystem_access": "host"}}


def test_member_override_prunes_reverted_slot(tmp_path: Path) -> None:
    """Reverting a slot back to the team value removes the stale override file."""
    _write_team_config(tmp_path)
    base = _member_intelligences(tmp_path, "alice")
    _write_yaml(base / "model_mapping.yml", {"default": "models/anthropic/claude.yml"})

    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={
            "default": "models/openai/gpt.yml",
            "openai": "models/openai/gpt.yml",
        },
        cli_agent_mapping={"default": "codex-cli.yml"},
        brain_mapping=_team_brain_assignments(),
    )
    IntelligenceConfigService().update_config(request)

    # The override now matches the team, so the file is pruned and the runtime
    # inherits the team slot again.
    assert not (base / "model_mapping.yml").exists()


def test_member_override_preserves_unsurfaced_def_fields(tmp_path: Path) -> None:
    """Saving a member override keeps definition fields the editor never shows.

    Regression: a blind rmtree + re-seed from the team dropped member-specific
    values (rate_limit, conversation_scope, ...) on every save.
    """
    _write_team_config(tmp_path)
    base = _member_intelligences(tmp_path, "alice")
    # Same surfaced class/id as the team, plus a hand-tuned rate_limit.
    _write_yaml(
        base / "models/openai/gpt.yml",
        {
            "model_class": "team.ModelClass",
            "parameters": {"id": "team-model-id"},
            "rate_limit": {"max_requests_per_minute": 3},
        },
    )
    _write_yaml(base / "model_mapping.yml", {"default": "models/openai/gpt.yml"})

    # The editor re-saves without touching the surfaced fields.
    request = IntelligenceConfigUpdateRequest(
        config_dir=tmp_path,
        person_id="alice",
        model_mapping={
            "default": "models/openai/gpt.yml",
            "openai": "models/openai/gpt.yml",
        },
        models=[
            ModelDefinition(
                path="models/openai/gpt.yml",
                provider="openai",
                model_class="team.ModelClass",
                model_id="team-model-id",
            ),
        ],
        cli_agent_mapping={"default": "codex-cli.yml"},
        brain_mapping=_team_brain_assignments(),
    )
    IntelligenceConfigService().update_config(request)

    stored = load_yaml_file(base / "models/openai/gpt.yml")
    assert stored["rate_limit"] == {"max_requests_per_minute": 3}


def test_member_override_update_preserves_policy_when_request_omits_it(
    tmp_path: Path,
) -> None:
    base = _member_intelligences(tmp_path, "alice")
    existing = {"codex": {"filesystem_access": "host"}}
    _write_yaml(base / "native_agent_policy.yml", existing)

    IntelligenceConfigService().update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=tmp_path,
            person_id="alice",
            model_mapping={},
            cli_agent_mapping={},
        )
    )

    assert load_yaml_file(base / "native_agent_policy.yml") == existing


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
