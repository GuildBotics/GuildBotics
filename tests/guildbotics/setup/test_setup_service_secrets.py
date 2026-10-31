"""Keychain-backed secret handling in the simple setup service.

These tests pin the stores to the OS keychain (via the ``fake_keyring``
fixture) and assert that secrets go there while non-secret values stay in
plain configuration files: a workspace's own keys in its store, and every
member's in the identities' store, which every workspace shares.
"""

from pathlib import Path

import pytest

from guildbotics.integrations.provider import SetupError
from guildbotics.setup.setup_service import (
    MEMBER_SECRET_NAMES,
    PersonSetupInput,
    ProjectSetupInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import load_yaml_file
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.person_id import IDENTITY_FILE, identity_members_dir
from guildbotics.utils.secret_store import (
    SECRETS_INDEX_FILENAME,
    KeyringSecretStore,
    identity_secret_store,
)


def _project_input(config_dir: Path, **overrides):
    payload: dict = {
        "config_dir": config_dir,
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "provider_api_keys": {"openai": "sk-secret"},
    }
    payload.update(overrides)
    return ProjectSetupInput(**payload)


def _person_input(config_dir: Path, **overrides):
    payload: dict = {
        "config_dir": config_dir,
        "person_type": "machine_user",
        "person_id": "alice",
        "person_name": "Alice",
        "is_active": True,
        "git_email": "1+alice@users.noreply.github.com",
        "roles": ["architect"],
    }
    payload.update(overrides)
    return PersonSetupInput(**payload)


def _github(**fields) -> dict:
    return {"github": {"account_type": "machine_user", "username": "alice", **fields}}


def _config_dir(tmp_path: Path) -> Path:
    return tmp_path / ".guildbotics" / "config"


def _identity_file(person_id: str = "alice") -> Path:
    return identity_members_dir() / person_id / IDENTITY_FILE


class TestProjectSecrets:
    def test_write_project_stores_api_key_in_keychain(self, fake_keyring, tmp_path):
        config_dir = _config_dir(tmp_path)

        SimpleProjectSetupService().write_project(_project_input(config_dir))

        assert (config_dir / SECRETS_INDEX_FILENAME).exists()
        assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "sk-secret"
        assert not (tmp_path / ".env").exists()

    def test_write_project_without_key_still_pins_backend(self, fake_keyring, tmp_path):
        config_dir = _config_dir(tmp_path)

        SimpleProjectSetupService().write_project(
            _project_input(config_dir, provider_api_keys={})
        )

        assert (config_dir / SECRETS_INDEX_FILENAME).exists()

    def test_read_project_config_sees_keychain_keys(self, fake_keyring, tmp_path):
        config_dir = _config_dir(tmp_path)
        service = SimpleProjectSetupService()
        service.write_project(_project_input(config_dir))

        snapshot = service.read_project_config(config_dir=config_dir)

        assert snapshot.provider_api_keys["openai"] is True

    def test_update_project_stores_new_key_in_keychain(self, fake_keyring, tmp_path):
        config_dir = _config_dir(tmp_path)
        service = SimpleProjectSetupService()
        service.write_project(_project_input(config_dir))

        service.update_project(
            ProjectSetupInput(
                config_dir=config_dir,
                language="en",
                llm_api_type="anthropic",
                provider_api_keys={"anthropic": "sk-ant-secret"},
            )
        )

        assert KeyringSecretStore(config_dir).get("ANTHROPIC_API_KEY") == (
            "sk-ant-secret"
        )
        assert not (tmp_path / ".env").exists()


class TestPersonSecrets:
    def _workspace(self, tmp_path: Path) -> Path:
        config_dir = _config_dir(tmp_path)
        config_dir.mkdir(parents=True)
        KeyringSecretStore(config_dir).ensure_initialized()
        return config_dir

    def test_save_person_stores_tokens_in_the_identities_keychain(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)

        result = SimplePersonSetupService().save_person(
            _person_input(
                config_dir,
                accounts={
                    **_github(access_token="ghp-secret", installation_id=42),
                    "slack": {"bot_token": "xoxb-secret"},
                },
            )
        )

        store = identity_secret_store()
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        assert store.get("ALICE_SLACK_BOT_TOKEN") == "xoxb-secret"
        assert KeyringSecretStore(config_dir).keys() == []
        identity = load_yaml_file(_identity_file())
        assert identity["account_info"]["github_installation_id"] == "42"
        assert "ghp-secret" not in _identity_file().read_text()
        assert store.location in {created.path for created in result.files}
        assert not (tmp_path / ".env").exists()

    def test_read_person_config_sees_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-secret"))
        )

        snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")

        assert snapshot.accounts["github"]["has_access_token"] is True
        assert snapshot.accounts["github"]["has_private_key"] is False
        assert snapshot.accounts["slack"]["has_bot_token"] is False

    def test_saving_under_another_id_is_another_member(self, fake_keyring, tmp_path):
        """A person ID is immutable: a form saved under another ID makes
        another member, which takes none of the first one's secrets."""
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-secret"))
        )

        service.save_person(_person_input(config_dir, person_id="alice-2"))

        store = identity_secret_store()
        assert store.keys() == ["ALICE_GITHUB_ACCESS_TOKEN"]
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        assert {path.name for path in identity_members_dir().iterdir()} == {
            "alice",
            "alice-2",
        }
        snapshot = service.read_person_config(
            config_dir=config_dir, person_id="alice-2"
        )
        assert snapshot.accounts["github"]["has_access_token"] is False

    def test_save_person_blank_token_keeps_existing_secret(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-secret"))
        )

        service.save_person(
            _person_input(config_dir, accounts=_github(access_token=""))
        )

        assert identity_secret_store().get("ALICE_GITHUB_ACCESS_TOKEN") == (
            "ghp-secret"
        )

    def test_save_person_copies_private_key_content_to_keychain(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        pem = "-----BEGIN RSA PRIVATE KEY-----\npem"

        SimplePersonSetupService().save_person(
            _person_input(
                config_dir,
                accounts=_github(account_type="github_apps", private_key=pem, app_id=7),
            )
        )

        store = identity_secret_store()
        assert store.get("ALICE_GITHUB_PRIVATE_KEY") == pem
        identity = load_yaml_file(_identity_file())
        assert identity["account_info"]["github_app_id"] == "7"
        assert pem not in _identity_file().read_text()

        snapshot = SimplePersonSetupService().read_person_config(
            config_dir=config_dir, person_id="alice"
        )
        assert snapshot.accounts["github"]["has_private_key"] is True

    def test_save_person_rejects_an_account_the_provider_refuses_before_writing(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)

        with pytest.raises(SetupError) as exc_info:
            SimplePersonSetupService().save_person(
                _person_input(
                    config_dir,
                    accounts=_github(
                        access_token="ghp-secret", app_registration_id="unknown"
                    ),
                )
            )

        assert exc_info.value.code == "github_app_registration_not_found"
        assert not (config_dir / "team" / "members" / "alice").exists()
        assert not _identity_file().parent.exists()
        assert identity_secret_store().keys() == []

    def test_save_person_rejects_an_account_the_provider_refuses_before_changing_the_member(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-old"))
        )
        files = (_identity_file(), config_dir / "team/members/alice/member.yml")
        before = {path: path.read_bytes() for path in files}

        with pytest.raises(SetupError) as exc_info:
            service.save_person(
                _person_input(
                    config_dir,
                    person_name="Alice 2",
                    accounts=_github(
                        access_token="ghp-new", app_registration_id="unknown"
                    ),
                )
            )

        assert exc_info.value.code == "github_app_registration_not_found"
        assert identity_secret_store().get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-old"
        assert {path: path.read_bytes() for path in files} == before

    def test_only_deleting_the_identity_removes_every_member_secret(
        self, fake_keyring, tmp_path
    ):
        """Every provider's member secrets stay while the member is only taken
        out of a workspace, and go with its identity, whichever provider the
        member's form named."""
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(_person_input(config_dir))
        KeyringSecretStore(config_dir).set("OPENAI_API_KEY", "workspace-key")
        store = identity_secret_store()
        for name in MEMBER_SECRET_NAMES:
            store.set(f"ALICE_{name}", f"value-{name}")

        service.remove_person(config_dir=config_dir, person_id="alice")

        assert sorted(store.keys()) == sorted(
            f"ALICE_{name}" for name in MEMBER_SECRET_NAMES
        )
        for name in MEMBER_SECRET_NAMES:
            assert store.get(f"ALICE_{name}") == f"value-{name}"

        service.delete_identity(person_id="alice")

        assert store.keys() == []
        assert KeyringSecretStore(config_dir).keys() == ["OPENAI_API_KEY"]

    def test_delete_identity_removes_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(
                config_dir,
                accounts={
                    **_github(access_token="ghp-secret"),
                    "slack": {"app_token": "xapp-secret"},
                },
            )
        )

        result = service.delete_identity(person_id="alice")

        store = identity_secret_store()
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None
        assert store.get("ALICE_SLACK_APP_TOKEN") is None
        assert store.keys() == []
        assert not _identity_file().parent.exists()
        assert [(created.path, created.action) for created in result.files] == [
            (store.location, "update"),
            (_identity_file(), "delete"),
        ]


class TestSecretStoreFailure:
    """A secret store that refuses a write leaves the member's files as they were."""

    def _workspace(self, tmp_path: Path) -> Path:
        config_dir = _config_dir(tmp_path)
        config_dir.mkdir(parents=True)
        KeyringSecretStore(config_dir).ensure_initialized()
        return config_dir

    def test_save_person_store_failure_leaves_member_config_untouched(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-old"))
        )
        files = (_identity_file(), config_dir / "team/members/alice/member.yml")
        before = {path: path.read_bytes() for path in files}

        def _raise(self, key, value):
            raise SecretStoreError("keychain is locked")

        monkeypatch.setattr(KeyringSecretStore, "set", _raise)
        with pytest.raises(SecretStoreError):
            service.save_person(
                _person_input(
                    config_dir,
                    person_name="Alice 2",
                    accounts=_github(access_token="ghp-new"),
                )
            )

        assert {path: path.read_bytes() for path in files} == before

    def test_save_person_store_failure_writes_no_member_config(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)

        def _raise(self, key, value):
            raise SecretStoreError("keychain is locked")

        monkeypatch.setattr(KeyringSecretStore, "set", _raise)
        with pytest.raises(SecretStoreError):
            SimplePersonSetupService().save_person(
                _person_input(config_dir, accounts={"slack": {"bot_token": "xoxb"}})
            )

        assert not (config_dir / "team" / "members" / "alice").exists()
        assert not _identity_file().parent.exists()

    def test_delete_identity_store_failure_keeps_the_identity(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.save_person(
            _person_input(config_dir, accounts=_github(access_token="ghp-old"))
        )

        def _raise(self, key):
            raise SecretStoreError("keychain is locked")

        with monkeypatch.context() as locked:
            locked.setattr(KeyringSecretStore, "delete", _raise)
            with pytest.raises(SecretStoreError):
                service.delete_identity(person_id="alice")

        assert _identity_file().exists()

        service.delete_identity(person_id="alice")
        assert not _identity_file().parent.exists()
