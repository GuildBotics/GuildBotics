"""Member namespaces must be unambiguous before any secret access."""

from pathlib import Path

import i18n
import pytest

from guildbotics.entities.team import Person
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.setup.setup_service import (
    PersonSetupInput,
    PersonUpdateInput,
    SetupServiceError,
    SimplePersonSetupService,
)
from guildbotics.utils.env_loader import read_workspace_secrets
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.person_id import MemberConfigError, person_secret_env_keys
from guildbotics.utils.secret_store import KeyringSecretStore


@pytest.fixture
def namespace_workspace(tmp_path, monkeypatch):
    config = tmp_path / ".guildbotics/config"
    (config / "team").mkdir(parents=True)
    (config / "team/project.yml").write_text("name: Test\nlanguage: en\n")
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    return config


def stored_member(config: Path, name: str) -> Path:
    path = config / "team/members" / name / "person.yml"
    path.parent.mkdir(parents=True)
    path.write_text(f"person_id: {name}\nname: Test\n", encoding="utf-8")
    return path


def member_input(config, **overrides):
    return PersonSetupInput(
        **{
            "config_dir": config,
            "person_id": "bob",
            "person_name": "Test",
            "person_type": "agent",
            "is_active": True,
            "github_username": "",
            "git_email": "",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "name, prefix",
    [
        ("alice-bak", "ALICE_BAK"),
        ("alice_bak", "ALICE_BAK"),
        ("Alice", "ALICE"),
        ("alıce", "ALICE"),
        ("aiko_1-2", "AIKO_1_2"),
    ],
)
def test_person_prefix_format_is_shared(tmp_path, name, prefix):
    assert person_secret_env_keys(tmp_path, name) == {
        "GITHUB_ACCESS_TOKEN": f"{prefix}_GITHUB_ACCESS_TOKEN",
        "GITHUB_PRIVATE_KEY": f"{prefix}_GITHUB_PRIVATE_KEY",
        "SLACK_BOT_TOKEN": f"{prefix}_SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN": f"{prefix}_SLACK_APP_TOKEN",
    }


def test_pending_alias_cannot_read_an_existing_members_slack_tokens(
    namespace_workspace, monkeypatch
):
    stored_member(namespace_workspace, "alice-bak")
    forbid_store(monkeypatch)
    assert SimplePersonSetupService().read_slack_tokens(
        config_dir=namespace_workspace, person_id="alice_bak"
    ) == ("", "")


def test_repair_can_store_new_credentials_at_unique_target(namespace_workspace):
    config = namespace_workspace
    stored_member(config, "alice-bak")
    stored_member(config, "alice_bak")
    store = KeyringSecretStore(config)
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-existing")
    SimplePersonSetupService().update_person(
        PersonUpdateInput(
            **member_input(
                config, person_id="repaired", slack_bot_token="fake-new"
            ).model_dump(),
            original_person_id="alice-bak",
        )
    )
    assert store.get("ALICE_BAK_SLACK_BOT_TOKEN") == "fake-existing"
    assert store.get("REPAIRED_SLACK_BOT_TOKEN") == "fake-new"


def forbid_store(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("ambiguous namespace touched the secret store")

    for operation in ("get", "set", "rename", "delete", "keys"):
        monkeypatch.setattr(KeyringSecretStore, operation, unexpected)


@pytest.mark.parametrize(
    "existing, target",
    [("alice-bak", "alice_bak"), ("Alice", "alice"), ("alıce", "alice")],
)
@pytest.mark.parametrize("action", ["create", "rename"])
@pytest.mark.parametrize("language", ["en", "ja"])
def test_save_refuses_other_namespace_owner_before_secrets(
    namespace_workspace, monkeypatch, existing, target, action, language
):
    config = namespace_workspace
    other = stored_member(config, existing)
    original = stored_member(config, "bob")
    before = {path: path.read_bytes() for path in (other, original)}
    forbid_store(monkeypatch)
    service = SimplePersonSetupService()
    previous = i18n.get("locale")
    i18n.set("locale", language)
    try:
        with pytest.raises(SetupServiceError) as error:
            data = member_input(
                config, person_id=target, slack_bot_token="fake-new-token"
            )
            if action == "create":
                service.write_person(data)
            else:
                service.update_person(
                    PersonUpdateInput(**data.model_dump(), original_person_id="bob")
                )
        assert error.value.code == "person_env_prefix_conflict"
        assert target in str(error.value) and existing in str(error.value)
        assert str(error.value) == t(
            "member_config.target_prefix_conflict", person_id=target, members=existing
        )
    finally:
        i18n.set("locale", previous)
    assert original.exists() and other.exists()
    assert {path: path.read_bytes() for path in (other, original)} == before
    assert {directory.name for directory in (config / "team/members").iterdir()} == {
        existing,
        "bob",
    }


@pytest.mark.parametrize("names", [("alice-bak", "alice_bak"), ("Alice", "alıce")])
def test_load_refuses_all_colliding_configs_even_invalid_ones(
    namespace_workspace, names
):
    paths = [stored_member(namespace_workspace, name) for name in names]
    paths[0].write_text("person_id: [", encoding="utf-8")
    with pytest.raises(MemberConfigError) as error:
        YamlTeamLoader(str(namespace_workspace / "team")).load()
    for path in paths:
        assert str(path) in str(error.value)


@pytest.mark.parametrize("action", ["snapshot", "slack", "rename", "delete"])
@pytest.mark.parametrize("names", [("alice-bak", "alice_bak"), ("Alice", "alıce")])
def test_repair_preserves_ambiguous_secrets_without_access(
    namespace_workspace, monkeypatch, tmp_path, names, action
):
    config = namespace_workspace
    paths = [stored_member(config, name) for name in names]
    store = KeyringSecretStore(config)
    keys = person_secret_env_keys(tmp_path, names[0]).values()
    for key in keys:
        store.set(key, "fake-existing-value")
    before = store.location.read_bytes()
    with monkeypatch.context() as guarded:
        forbid_store(guarded)
        service = SimplePersonSetupService()
        if action == "snapshot":
            snapshot = service.read_person_config(config_dir=config, person_id=names[0])
            assert not any(
                [
                    snapshot.has_github_access_token,
                    snapshot.has_github_private_key,
                    snapshot.has_slack_bot_token,
                    snapshot.has_slack_app_token,
                ]
            )
        elif action == "slack":
            assert service.read_slack_tokens(config_dir=config, person_id=names[0]) == (
                "",
                "",
            )
        elif action == "rename":
            service.update_person(
                PersonUpdateInput(
                    **member_input(config, person_id="repaired").model_dump(),
                    original_person_id=names[0],
                )
            )
            assert not paths[0].exists()
            assert (config / "team/members/repaired/person.yml").exists()
        else:
            service.delete_person(config_dir=config, person_id=names[0])
            assert not paths[0].exists()
    assert paths[1].exists()
    assert store.location.read_bytes() == before
    for key in keys:
        assert store.get(key) == "fake-existing-value"


@pytest.mark.parametrize("access", ["get", "has", "private_key"])
def test_live_person_refuses_secret_after_collision_appears(
    namespace_workspace, monkeypatch, access
):
    from guildbotics.integrations.github.github_utils import get_person_private_key_pem

    person = Person(person_id="alice-bak", name="Test")
    stored_member(namespace_workspace, "alice-bak")
    stored_member(namespace_workspace, "alice_bak")
    monkeypatch.setenv("ALICE_BAK_SLACK_BOT_TOKEN", "fake-env-value")
    forbid_store(monkeypatch)
    with pytest.raises(MemberConfigError):
        if access == "get":
            person.get_secret("SLACK_BOT_TOKEN")
        elif access == "has":
            person.has_secret("SLACK_BOT_TOKEN")
        else:
            get_person_private_key_pem(person)


def test_bulk_secret_read_skips_ambiguous_keys_only(namespace_workspace, monkeypatch):
    config = namespace_workspace
    stored_member(config, "alice-bak")
    stored_member(config, "alice_bak")
    stored_member(config, "alice_bak_other")
    store = KeyringSecretStore(config)
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-ambiguous")
    store.set("ALICE_BAK_OTHER_SLACK_BOT_TOKEN", "fake-unique")
    original = KeyringSecretStore.get

    def guarded_get(self, key):
        assert key != "ALICE_BAK_SLACK_BOT_TOKEN"
        return original(self, key)

    monkeypatch.setattr(KeyringSecretStore, "get", guarded_get)
    assert read_workspace_secrets() == {
        "ALICE_BAK_OTHER_SLACK_BOT_TOKEN": "fake-unique"
    }
