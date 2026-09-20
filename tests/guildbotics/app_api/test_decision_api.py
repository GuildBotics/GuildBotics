"""The editor and save boundary use the same device readiness as execution."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from guildbotics.app_api.api import create_app
from guildbotics.app_api.intelligences import IntelligenceConfigService
from guildbotics.app_api.models import IntelligenceConfigUpdateRequest
from guildbotics.editions.simple.setup_service import SetupServiceError
from guildbotics.intelligences.decisions import engines, preparation, settings
from guildbotics.intelligences.decisions.models import DecisionConfig, Evaluation
from guildbotics.utils.secret_store import KeyringSecretStore

HEADERS = {"X-GuildBotics-Session-Token": "test"}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    root = tmp_path / ".guildbotics/config"
    root.mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(root))
    monkeypatch.delenv(settings.JEV_KEY, raising=False)
    monkeypatch.setattr(
        "guildbotics.app_api.api._resolve_existing_config_dir", lambda _: root
    )
    monkeypatch.setattr(
        settings,
        "device_status",
        lambda: SimpleNamespace(
            refusal="not prepared", tool=lambda _: SimpleNamespace(refusal="")
        ),
    )
    return root, TestClient(create_app(session_token="test"))


def test_registration_refresh_save_and_deletion(configured):
    root, client = configured
    config = {"engine": "jev", "provider": "", "model": "jev-1.13.0"}
    body = {"config": config}
    assert client.post("/intelligences/decisions/status", json=body).status_code == 401
    assert (
        client.post(
            "/intelligences/decisions/status", headers=HEADERS, json=body
        ).json()["state"]
        == "missing"
    )
    response = client.post(
        "/intelligences/decisions/credential",
        headers=HEADERS,
        json={"value": "unique-test-credential"},
    )
    assert response.status_code == 200
    assert response.json()["state"] == "unverified"
    assert "unique-test-credential" not in response.text
    options = client.get("/intelligences/decisions/options", headers=HEADERS).json()[
        "options"
    ]
    assert next(o for o in options if o["engine"] == "jev")["available"]
    request = IntelligenceConfigUpdateRequest(
        config_dir=root, decision=DecisionConfig(**config)
    )
    IntelligenceConfigService().update_config(request)
    assert settings.read_config(root) == request.decision
    KeyringSecretStore(root).delete(settings.JEV_KEY)
    assert not client.post(
        "/intelligences/decisions/status", headers=HEADERS, json=body
    ).json()["available"]
    assert settings.read_config(root) == request.decision


def test_malformed_credential_never_echoes_its_input(configured):
    _, client = configured
    response = client.post(
        "/intelligences/decisions/credential",
        headers=HEADERS,
        json={"value": ["private-credential"]},
    )
    assert response.status_code == 422
    assert "private-credential" not in response.text


@pytest.mark.parametrize("model", ["", "gpt-5", "claude-sonnet"])
def test_wrong_model_is_rejected_before_writing(configured, model):
    root, _ = configured
    KeyringSecretStore(root).set(settings.JEV_KEY, "test-key")
    with pytest.raises(SetupServiceError):
        IntelligenceConfigService().update_config(
            IntelligenceConfigUpdateRequest(
                config_dir=root, decision=DecisionConfig(engine="jev", model=model)
            )
        )
    assert not (root / "intelligences/decision.yml").exists()


@pytest.mark.parametrize(
    "status,state", [(401, "authentication_error"), (503, "connection_error")]
)
def test_connection_errors_do_not_erase_credentials(
    configured, monkeypatch, status, state
):
    root, client = configured
    KeyringSecretStore(root).set(settings.JEV_KEY, "test-key")

    async def failed(*args):
        response = httpx.Response(
            status, request=httpx.Request("GET", "https://api.typesafe.ai/v1/models")
        )
        response.raise_for_status()

    monkeypatch.setattr(preparation, "jev_request", failed)
    body = {"config": {"engine": "jev", "model": "jev-latest"}}
    response = client.post("/intelligences/decisions/check", headers=HEADERS, json=body)
    assert response.json()["status"]["state"] == state
    assert KeyringSecretStore(root).get(settings.JEV_KEY) == "test-key"
    assert (
        client.post(
            "/intelligences/decisions/status", headers=HEADERS, json=body
        ).json()["state"]
        == state
    )


def test_member_inherits_and_overrides_as_one_selection(configured):
    root, _ = configured
    service = IntelligenceConfigService()
    KeyringSecretStore(root).set(settings.JEV_KEY, "test-key")
    team = DecisionConfig(engine="jev", model="jev-latest")
    member = DecisionConfig(engine="jev", model="jev-1.13.0")
    service.update_config(
        IntelligenceConfigUpdateRequest(config_dir=root, decision=team)
    )
    assert settings.read_config(root, "alice") == team
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root, person_id="alice", decision=member
        )
    )
    assert settings.read_config(root, "alice") == member
    service.update_config(
        IntelligenceConfigUpdateRequest(
            config_dir=root, person_id="alice", inherit_team_defaults=True
        )
    )
    assert settings.read_config(root, "alice") == team


@pytest.mark.asyncio
async def test_verified_requires_both_answer_types(configured, monkeypatch):
    root, _ = configured
    KeyringSecretStore(root).set(settings.JEV_KEY, "test-key")

    async def models(*args):
        return {"models": [{"name": "jev-latest"}]}

    async def evaluated(config, state, questions, **kwargs):
        assert {q.type for q in questions.values()} == {"noul", "choice"}
        return Evaluation(model="jev-1.13.0")

    monkeypatch.setattr(preparation, "jev_request", models)
    monkeypatch.setattr(preparation, "evaluate", evaluated)
    result = await preparation.check(
        DecisionConfig(engine="jev", model="jev-latest"), root
    )
    assert result["status"].state == "verified"
