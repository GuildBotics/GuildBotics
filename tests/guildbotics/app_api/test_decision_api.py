"""Chat judgment uses the normal brain assignment and inheritance."""

import logging
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.intelligences import (
    CLI_BRAIN_CLASS,
    JEV_BRAIN_CLASS,
    IntelligenceConfigService,
)
from guildbotics.app_api.models import BrainAssignment, IntelligenceConfigUpdateRequest
from guildbotics.editions.simple.setup_service import SetupServiceError
from guildbotics.editions.simple.simple_brain_factory import SimpleBrainFactory
from guildbotics.intelligences.brains.jev import JEV_KEY, JevBrain
from guildbotics.intelligences.decisions import preparation
from guildbotics.intelligences.decisions.models import DecisionConfig, Evaluation
from guildbotics.utils.secret_store import KeyringSecretStore

HEADERS = {"X-GuildBotics-Session-Token": "test"}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / ".guildbotics/config"
    root.mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(root))
    monkeypatch.setattr(
        "guildbotics.app_api.api._resolve_existing_config_dir", lambda _: root
    )
    return root, TestClient(create_app(session_token="test"))


def assignment(model):
    return BrainAssignment(
        name="chat_decision", brain_class=JEV_BRAIN_CLASS, engine="jev", target=model
    )


def test_registration_and_deletion(configured):
    root, client = configured
    assert (
        client.get("/intelligences/decisions/options").status_code
        == HTTPStatus.UNAUTHORIZED
    )
    assert not client.get("/intelligences/decisions/options", headers=HEADERS).json()[
        "credential_present"
    ]
    response = client.post(
        "/intelligences/decisions/credential",
        headers=HEADERS,
        json={"value": "private-test-key"},
    )
    assert response.json()["state"] == "unverified"
    assert "private-test-key" not in response.text
    assert client.get("/intelligences/decisions/options", headers=HEADERS).json()[
        "credential_present"
    ]
    KeyringSecretStore(root).delete(JEV_KEY)
    assert not client.get("/intelligences/decisions/options", headers=HEADERS).json()[
        "credential_present"
    ]


def test_malformed_credential_never_echoes_its_input(configured):
    _, client = configured
    response = client.post(
        "/intelligences/decisions/credential",
        headers=HEADERS,
        json={"value": ["private-credential"]},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "private-credential" not in response.text


@pytest.mark.parametrize("model", ["", "gpt-5", "claude-sonnet"])
def test_wrong_jev_model_is_rejected(configured, model):
    root, _ = configured
    with pytest.raises(SetupServiceError):
        IntelligenceConfigService().update_config(
            IntelligenceConfigUpdateRequest(
                config_dir=root, brain_mapping=[assignment(model)]
            )
        )
    assert not (root / "intelligences/brain_mapping.yml").exists()


def test_member_inherits_and_overrides_through_brain_factory(configured):
    root, _ = configured
    service = IntelligenceConfigService()
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root, brain_mapping=[assignment("jev-latest")]
        )
    )
    factory = SimpleBrainFactory()

    def selected():
        return factory.create_brain(
            "alice",
            "chat_decision",
            "",
            logging.getLogger(),
            config={"brain": "chat_decision"},
        )

    assert isinstance(selected(), JevBrain)
    assert selected().model == "jev-latest"
    assert not (root / "intelligences/decision.yml").exists()
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root, person_id="alice", brain_mapping=[assignment("jev-1.13.0")]
        )
    )
    assert selected().model == "jev-1.13.0"
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root, person_id="alice", inherit_team_defaults=True
        )
    )
    assert selected().model == "jev-latest"


@pytest.mark.parametrize("tool", ["codex", "claude", "grok", "copilot", "antigravity"])
def test_cli_assignment_uses_existing_slot(configured, tool):
    root, _ = configured
    service = IntelligenceConfigService()
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root,
            cli_agent_mapping={"judge": f"cli_agents/{tool}/default.yml"},
            brain_mapping=[
                BrainAssignment(
                    name="chat_decision",
                    brain_class=CLI_BRAIN_CLASS,
                    engine="cli",
                    target="judge",
                )
            ],
        )
    )
    brain = SimpleBrainFactory().create_brain(
        "alice",
        "chat_decision",
        "",
        logging.getLogger(),
        config={"brain": "chat_decision"},
    )
    assert brain.cli_agent == "judge"
    assert tool in brain.executable_info.adapter
    selection = next(
        a
        for a in service.read_config(config_dir=root).brain_mapping
        if a.name == "chat_decision"
    )
    assert selection.engine == "cli" and selection.target == "judge"


@pytest.mark.asyncio
async def test_check_uses_saved_brain_and_both_answer_types(configured, monkeypatch):
    root, _ = configured

    async def evaluated(config, state, questions, **kwargs):
        assert config.brain == "chat_decision"
        assert {q.type for q in questions.values()} == {"noul", "choice"}
        return Evaluation(model="effective-model")

    monkeypatch.setattr(preparation, "evaluate", evaluated)
    result = await preparation.check(DecisionConfig(), root)
    assert result == {"state": "verified", "model": "effective-model"}
