"""Member namespaces must be unambiguous before any secret access.

Every member's secrets live in the one identities' store, so the namespace is
the identities': two members collide whether or not they share a workspace.
"""

from pathlib import Path

import i18n
import pytest

from guildbotics.entities.team import Person
from guildbotics.integrations.factory import PROVIDERS
from guildbotics.integrations.provider import SetupError
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.setup.setup_service import (
    MEMBER_SECRET_NAMES,
    PersonSetupInput,
    SimplePersonSetupService,
)
from guildbotics.utils.env_loader import read_workspace_secrets
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.person_id import (
    IDENTITY_FILE,
    MemberConfigError,
    PersonEnvPrefixConflictError,
    ambiguous_person_secret,
    identity_members_dir,
    is_member_secret,
    person_secret_prefix,
)
from guildbotics.utils.secret_store import KeyringSecretStore, identity_secret_store
from tests.guildbotics.members import write_member


@pytest.fixture
def namespace_workspace(tmp_path, monkeypatch):
    config = tmp_path / ".guildbotics/config"
    (config / "team").mkdir(parents=True)
    (config / "team/project.yml").write_text("name: Test\nlanguage: en\n")
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config))
    return config


def stored_identity(name: str) -> Path:
    """An identity that no workspace has; returns its file."""
    path = identity_members_dir() / name / IDENTITY_FILE
    path.parent.mkdir(parents=True)
    path.write_text(f"person_id: {name}\nname: Test\n", encoding="utf-8")
    return path


def stored_member(config: Path, name: str) -> Path:
    """A member of the workspace; returns its identity file."""
    write_member(config, {"person_id": name, "name": "Test"})
    return identity_members_dir() / name / IDENTITY_FILE


def member_input(config, **overrides):
    return PersonSetupInput(
        **{
            "config_dir": config,
            "person_id": "bob",
            "person_name": "Test",
            "person_type": "agent",
            "is_active": True,
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
def test_person_prefix_format_is_shared(name, prefix):
    assert person_secret_prefix(name) == f"{prefix}_"


@pytest.mark.parametrize("in_workspace", [True, False])
def test_prefix_is_refused_while_another_identity_shares_it(
    namespace_workspace, in_workspace
):
    if in_workspace:
        stored_member(namespace_workspace, "alice-bak")
    else:
        stored_identity("alice-bak")

    with pytest.raises(PersonEnvPrefixConflictError) as error:
        person_secret_prefix("alice_bak")

    assert error.value.conflicts == [identity_members_dir() / "alice-bak"]
    # The identity holding the prefix alone owns it.
    assert person_secret_prefix("alice-bak") == "ALICE_BAK_"


def test_a_key_is_ambiguous_by_the_member_of_its_longest_prefix(
    namespace_workspace,
):
    stored_member(namespace_workspace, "alice-bak")
    for name in ("alice_bak", "alice_bak_other", "bob"):
        stored_identity(name)

    ambiguous = ambiguous_person_secret()

    assert ambiguous("ALICE_BAK_SLACK_BOT_TOKEN") is True
    assert ambiguous("ALICE_BAK_OTHER_SLACK_BOT_TOKEN") is False
    assert ambiguous("BOB_SLACK_BOT_TOKEN") is False
    assert ambiguous("OPENAI_API_KEY") is False


def test_a_member_secret_is_one_under_a_stored_identitys_prefix(namespace_workspace):
    stored_member(namespace_workspace, "alice")
    stored_identity("bob")

    assert is_member_secret("ALICE_SLACK_BOT_TOKEN") is True
    assert is_member_secret("BOB_GITHUB_ACCESS_TOKEN") is True
    assert is_member_secret("OPENAI_API_KEY") is False


def test_an_unrelated_member_saves_credentials_while_others_collide(
    namespace_workspace,
):
    config = namespace_workspace
    stored_member(config, "alice-bak")
    stored_identity("alice_bak")
    store = identity_secret_store()
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-existing")

    SimplePersonSetupService().save_person(
        member_input(
            config, person_id="carol", accounts={"slack": {"bot_token": "fake-new"}}
        )
    )

    assert store.get("ALICE_BAK_SLACK_BOT_TOKEN") == "fake-existing"
    assert store.get("CAROL_SLACK_BOT_TOKEN") == "fake-new"


def test_deleting_one_identity_gives_the_other_its_keys(namespace_workspace):
    """A collision is repaired by deleting one of the identities. The keys,
    untouched while they were nobody's own, stay for the one that remains."""
    config = namespace_workspace
    stored_member(config, "alice-bak")
    stored_identity("alice_bak")
    store = identity_secret_store()
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-existing")
    service = SimplePersonSetupService()

    service.delete_identity(person_id="alice_bak")

    assert store.get("ALICE_BAK_SLACK_BOT_TOKEN") == "fake-existing"
    [member] = YamlTeamLoader(str(config / "team")).load().members
    assert member.person_id == "alice-bak"
    service.save_person(
        member_input(
            config, person_id="alice-bak", accounts={"slack": {"bot_token": "fake-new"}}
        )
    )
    assert store.get("ALICE_BAK_SLACK_BOT_TOKEN") == "fake-new"


def forbid_store(monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("ambiguous namespace touched the secret store")

    for operation in ("get", "set", "rename", "delete", "keys"):
        monkeypatch.setattr(KeyringSecretStore, operation, unexpected)


@pytest.mark.parametrize(
    "existing, target",
    [("alice-bak", "alice_bak"), ("Alice", "alice"), ("alıce", "alice")],
)
@pytest.mark.parametrize("in_workspace", [True, False])
@pytest.mark.parametrize("language", ["en", "ja"])
def test_save_refuses_other_namespace_owner_before_secrets(
    namespace_workspace, monkeypatch, existing, target, in_workspace, language
):
    config = namespace_workspace
    if in_workspace:
        stored_member(config, existing)
    else:
        stored_identity(existing)
    stored_member(config, "bob")
    files = [
        path
        for root in (config, identity_members_dir())
        for path in root.rglob("*")
        if path.is_file()
    ]
    before = {path: path.read_bytes() for path in files}
    forbid_store(monkeypatch)
    service = SimplePersonSetupService()
    previous = i18n.get("locale")
    i18n.set("locale", language)
    try:
        with pytest.raises(SetupError) as error:
            service.save_person(
                member_input(
                    config,
                    person_id=target,
                    accounts={"slack": {"bot_token": "fake-new-token"}},
                )
            )
        assert error.value.code == "person_env_prefix_conflict"
        assert target in str(error.value) and existing in str(error.value)
        assert str(error.value) == t(
            "member_config.target_prefix_conflict", person_id=target, members=existing
        )
    finally:
        i18n.set("locale", previous)
    assert {path: path.read_bytes() for path in files} == before
    assert {directory.name for directory in identity_members_dir().iterdir()} == {
        existing,
        "bob",
    }
    assert {directory.name for directory in (config / "team/members").iterdir()} == (
        {existing, "bob"} if in_workspace else {"bob"}
    )


@pytest.mark.parametrize("other_in_workspace", [True, False])
@pytest.mark.parametrize("names", [("alice-bak", "alice_bak"), ("Alice", "alıce")])
def test_load_refuses_all_colliding_configs_even_invalid_ones(
    namespace_workspace, names, other_in_workspace
):
    paths = [
        stored_member(namespace_workspace, names[0]),
        (
            stored_member(namespace_workspace, names[1])
            if other_in_workspace
            else stored_identity(names[1])
        ),
    ]
    paths[0].write_text("person_id: [", encoding="utf-8")
    with pytest.raises(MemberConfigError) as error:
        YamlTeamLoader(str(namespace_workspace / "team")).load()
    for path in paths:
        assert str(path) in str(error.value)


@pytest.mark.parametrize("action", ["snapshot", "remove", "delete"])
@pytest.mark.parametrize("names", [("alice-bak", "alice_bak"), ("Alice", "alıce")])
def test_repair_preserves_ambiguous_secrets_without_access(
    namespace_workspace, monkeypatch, names, action
):
    config = namespace_workspace
    prefix = person_secret_prefix(names[0])
    paths = [stored_member(config, name) for name in names]
    store = identity_secret_store()
    keys = [prefix + name for name in MEMBER_SECRET_NAMES]
    for key in keys:
        store.set(key, "fake-existing-value")
    before = store.location.read_bytes()
    with monkeypatch.context() as guarded:
        forbid_store(guarded)
        service = SimplePersonSetupService()
        if action == "snapshot":
            snapshot = service.read_person_config(config_dir=config, person_id=names[0])
            assert snapshot.secret_env_keys == {}
            assert set(snapshot.accounts) == {
                p.name for p in PROVIDERS.values() if p.setup and p.setup.member
            }
            assert not any(
                value
                for view in snapshot.accounts.values()
                for field, value in view.items()
                if field.startswith("has_")
            )
        elif action == "remove":
            service.remove_person(config_dir=config, person_id=names[0])
            assert not (config / "team/members" / names[0]).exists()
            assert paths[0].exists()
        else:
            service.delete_identity(person_id=names[0])
            assert not paths[0].parent.exists()
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
    # The other identity is in no workspace: it still shares the store.
    stored_identity("alice_bak")
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
    stored_identity("alice_bak")
    stored_identity("alice_bak_other")
    store = identity_secret_store()
    store.set("ALICE_BAK_SLACK_BOT_TOKEN", "fake-ambiguous")
    store.set("ALICE_BAK_OTHER_SLACK_BOT_TOKEN", "fake-unique")
    KeyringSecretStore(config).set("OPENAI_API_KEY", "fake-workspace")
    original = KeyringSecretStore.get

    def guarded_get(self, key):
        assert key != "ALICE_BAK_SLACK_BOT_TOKEN"
        return original(self, key)

    monkeypatch.setattr(KeyringSecretStore, "get", guarded_get)
    assert read_workspace_secrets() == {
        "ALICE_BAK_OTHER_SLACK_BOT_TOKEN": "fake-unique",
        "OPENAI_API_KEY": "fake-workspace",
    }
