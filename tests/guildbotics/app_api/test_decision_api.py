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


@pytest.mark.parametrize(
    "model", ["", "gpt-5", "claude-sonnet", "jev-1.13.0", "jev-preview"]
)
def test_wrong_jev_model_is_rejected(configured, model):
    root, _ = configured
    with pytest.raises(SetupServiceError):
        IntelligenceConfigService().update_config(
            IntelligenceConfigUpdateRequest(
                config_dir=root, brain_mapping=[assignment(model)]
            )
        )
    assert not (root / "intelligences/brain_mapping.yml").exists()


@pytest.mark.parametrize("name", ["default", "agent", "translate"])
def test_jev_cannot_be_assigned_to_text_commands(configured, name):
    root, _ = configured
    with pytest.raises(SetupServiceError, match="only supported for chat_decision"):
        IntelligenceConfigService().update_config(
            IntelligenceConfigUpdateRequest(
                config_dir=root,
                brain_mapping=[
                    assignment("jev-latest").model_copy(update={"name": name})
                ],
            )
        )
    assert not (root / "intelligences/brain_mapping.yml").exists()


def test_member_rejects_cli_override_and_keeps_inherited_assignment(configured):
    root, _ = configured
    service = IntelligenceConfigService()
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root,
            cli_agent_mapping={"default": "cli_agents/codex/default.yml"},
            brain_mapping=[assignment("jev-latest")],
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
    with pytest.raises(SetupServiceError, match="not supported for chat_decision"):
        service.update_config(
            IntelligenceConfigUpdateRequest(
                config_dir=root,
                person_id="alice",
                cli_agent_mapping={"default": "cli_agents/codex/default.yml"},
                brain_mapping=[
                    BrainAssignment(
                        name="chat_decision",
                        brain_class=CLI_BRAIN_CLASS,
                        engine="cli",
                        target="default",
                    )
                ],
            )
        )
    assert not (root / "team/members/alice/intelligences/brain_mapping.yml").exists()
    assert selected().model == "jev-latest"


@pytest.mark.parametrize("tool", ["codex", "claude", "grok", "copilot", "antigravity"])
def test_cli_assignment_cannot_be_saved_for_chat_decision(configured, tool):
    root, _ = configured
    service = IntelligenceConfigService()
    with pytest.raises(SetupServiceError, match="not supported for chat_decision"):
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
    assert not (root / "intelligences/brain_mapping.yml").exists()


def test_member_model_resolves_with_inherited_assignment(
    configured,
):
    from guildbotics.app_api.intelligences import AGNO_BRAIN_CLASS
    from guildbotics.app_api.models import ModelDefinition

    root, _ = configured
    service = IntelligenceConfigService()
    path = "models/openai/judge.yml"
    team_model = ModelDefinition(
        path=path,
        provider="openai",
        model_class="agno.models.openai.OpenAIChat",
        parameters={"id": "team-model"},
    )
    assigned = BrainAssignment(
        name="chat_decision", brain_class=AGNO_BRAIN_CLASS, engine="llm", target="judge"
    )
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root,
            model_mapping={"judge": path},
            models=[team_model],
            brain_mapping=[assigned],
        )
    )
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root,
            person_id="alice",
            model_mapping={"judge": path},
            models=[
                team_model.model_copy(update={"parameters": {"id": "member-model"}})
            ],
            brain_mapping=[assigned],
        )
    )
    brain = SimpleBrainFactory().create_brain(
        "alice",
        "chat_decision",
        "",
        logging.getLogger(),
        config={"brain": "chat_decision"},
    )
    assert brain.model_config.parameters["id"] == "member-model"
