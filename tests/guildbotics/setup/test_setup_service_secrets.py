"""Keychain-backed secret handling in the simple setup service.

These tests pin the workspace to the OS keychain (via the ``fake_keyring``
fixture) and assert that secrets go there while non-secret values stay in
plain configuration files.
"""

from pathlib import Path

import pytest

from guildbotics.setup.setup_service import (
    PersonSetupInput,
    PersonUpdateInput,
    ProjectSetupInput,
    ProjectUpdateInput,
    SetupServiceError,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import dump_yaml, load_yaml_file
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import (
    SECRETS_INDEX_FILENAME,
    KeyringSecretStore,
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
        "github_username": "alice",
        "git_email": "1+alice@users.noreply.github.com",
        "roles": ["architect"],
    }
    payload.update(overrides)
    return PersonSetupInput(**payload)


def _config_dir(tmp_path: Path) -> Path:
    return tmp_path / ".guildbotics" / "config"


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
            ProjectUpdateInput(
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

    def test_write_person_stores_tokens_in_keychain(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)

        result = SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                github_access_token="ghp-secret",
                slack_bot_token="xoxb-secret",
                github_installation_id=42,
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        assert store.get("ALICE_SLACK_BOT_TOKEN") == "xoxb-secret"
        person = load_yaml_file(config_dir / "team/members/alice/person.yml")
        assert person["account_info"]["github_installation_id"] == "42"
        assert store.location in {created.path for created in result.files}
        assert not (tmp_path / ".env").exists()

    def test_read_person_config_sees_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, github_access_token="ghp-secret")
        )

        snapshot = service.read_person_config(config_dir=config_dir, person_id="alice")

        assert snapshot.has_github_access_token is True
        assert snapshot.has_slack_bot_token is False

    @pytest.mark.parametrize(
        "original, renamed", [("alice", "alice-2"), ("Alice", "alice")]
    )
    def test_update_person_rename_moves_keychain_tokens(
        self, fake_keyring, tmp_path, original, renamed
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, github_access_token="ghp-secret")
        )
        members = config_dir / "team/members"
        if original != "alice":
            (members / "alice").rename(members / original)
        project_file = config_dir / "team/project.yml"
        project_file.write_text(dump_yaml({"default_person_id": original}))

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir).model_dump(),
                    "original_person_id": original,
                    "person_id": renamed,
                    "person_name": "Alice 2",
                }
            )
        )

        store = KeyringSecretStore(config_dir)
        prefix = "ALICE_2" if renamed == "alice-2" else "ALICE"
        assert store.get(f"{prefix}_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        if renamed == "alice-2":
            assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None
        assert [child.name for child in members.iterdir()] == [renamed]
        assert load_yaml_file(project_file)["default_person_id"] == renamed

    def test_update_person_blank_token_keeps_existing_secret(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, github_access_token="ghp-secret")
        )

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir).model_dump(),
                    "original_person_id": "alice",
                    "github_access_token": "",
                }
            )
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_ACCESS_TOKEN") == (
            "ghp-secret"
        )

    def test_write_person_copies_private_key_content_to_keychain(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        pem_file = tmp_path / "alice.pem"
        pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\npem\n")

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                github_private_key_path=pem_file,
                github_app_id=7,
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_PRIVATE_KEY") == pem_file.read_text()
        person = load_yaml_file(config_dir / "team/members/alice/person.yml")
        assert person["account_info"]["github_app_id"] == "7"
        assert pem_file.exists()

        snapshot = SimplePersonSetupService().read_person_config(
            config_dir=config_dir, person_id="alice"
        )
        assert snapshot.has_github_private_key is True

    def test_write_person_stores_the_key_it_is_given_over_a_key_file(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                github_private_key_path=tmp_path / "missing.pem",
                github_app_id=7,
            ),
            github_private_key="-----BEGIN RSA PRIVATE KEY-----\nheld\n",
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_PRIVATE_KEY") == (
            "-----BEGIN RSA PRIVATE KEY-----\nheld\n"
        )

    def test_update_person_stores_the_key_it_is_given(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir))

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir).model_dump(),
                    "original_person_id": "alice",
                }
            ),
            github_private_key="pem-content",
        )

        assert (
            KeyringSecretStore(config_dir).get("ALICE_GITHUB_PRIVATE_KEY")
            == "pem-content"
        )

    @pytest.mark.parametrize("content", [None, b"\xff\xfe"])
    def test_write_person_rejects_an_unreadable_key_file_before_writing(
        self, fake_keyring, tmp_path, content
    ):
        config_dir = self._workspace(tmp_path)
        pem_file = tmp_path / "alice.pem"
        if content is not None:
            pem_file.write_bytes(content)

        with pytest.raises(SetupServiceError) as exc_info:
            SimplePersonSetupService().write_person(
                _person_input(
                    config_dir,
                    github_access_token="ghp-secret",
                    github_private_key_path=pem_file,
                )
            )

        assert exc_info.value.code == "github_private_key_unreadable"
        assert not (config_dir / "team" / "members" / "alice").exists()
        assert KeyringSecretStore(config_dir).keys() == []

    def test_update_person_rejects_an_unreadable_key_file_before_renaming(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, github_access_token="ghp-old"))

        with pytest.raises(SetupServiceError) as exc_info:
            service.update_person(
                PersonUpdateInput(
                    **{
                        **_person_input(config_dir).model_dump(),
                        "original_person_id": "alice",
                        "person_id": "alice-2",
                        "github_private_key_path": tmp_path / "missing.pem",
                    }
                )
            )

        assert exc_info.value.code == "github_private_key_unreadable"
        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-old"
        assert not (config_dir / "team" / "members" / "alice-2").exists()

    def test_update_person_rename_moves_private_key_content(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir))
        KeyringSecretStore(config_dir).set("ALICE_GITHUB_PRIVATE_KEY", "pem-content")

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir).model_dump(),
                    "original_person_id": "alice",
                    "person_id": "alice-2",
                    "person_name": "Alice 2",
                }
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_2_GITHUB_PRIVATE_KEY") == "pem-content"
        assert store.get("ALICE_GITHUB_PRIVATE_KEY") is None

    def test_delete_person_removes_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(
                config_dir,
                github_access_token="ghp-secret",
                slack_app_token="xapp-secret",
            )
        )

        service.delete_person(config_dir=config_dir, person_id="alice")

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None
        assert store.get("ALICE_SLACK_APP_TOKEN") is None
        assert store.keys() == []


class TestStaleKeyMetadata:
    """Shared key metadata follows rename/delete even when the local value
    is stale (another device holds the current generation)."""

    def _workspace(self, tmp_path: Path) -> Path:
        config_dir = _config_dir(tmp_path)
        config_dir.mkdir(parents=True)
        KeyringSecretStore(config_dir).ensure_initialized()
        return config_dir

    def _make_stale(self, config_dir: Path, key: str) -> None:
        store = KeyringSecretStore(config_dir)
        # The value written here reached the hub as generation 1 ...
        value = store.get(key)
        assert value is not None
        store.confirm_shared({key: 1}, sent={key: value})
        # ... and another device then published generation 2. Synchronization
        # delivers the bumped index as a checkout, not through a writer, so it
        # is written directly.
        index = load_yaml_file(store.location)
        index["keys"][key]["generation"] = 2
        store.location.write_text(dump_yaml(index), encoding="utf-8")
        assert store.get(key) is None

    def test_update_person_rename_moves_stale_key_metadata(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, github_access_token="ghp-old"))
        self._make_stale(config_dir, "ALICE_GITHUB_ACCESS_TOKEN")

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir).model_dump(),
                    "original_person_id": "alice",
                    "person_id": "alice-2",
                }
            )
        )

        store = KeyringSecretStore(config_dir)
        assert "ALICE_GITHUB_ACCESS_TOKEN" not in store.keys()
        assert "ALICE_2_GITHUB_ACCESS_TOKEN" in store.keys()
        assert "ALICE_2_GITHUB_ACCESS_TOKEN" in store.stale_keys()

    def test_delete_person_removes_stale_key_metadata(self, fake_keyring, tmp_path):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, github_access_token="ghp-old"))
        self._make_stale(config_dir, "ALICE_GITHUB_ACCESS_TOKEN")

        service.delete_person(config_dir=config_dir, person_id="alice")

        assert "ALICE_GITHUB_ACCESS_TOKEN" not in KeyringSecretStore(config_dir).keys()

    def test_update_person_store_failure_leaves_member_config_untouched(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, github_access_token="ghp-old"))

        def _raise(self, old_key, new_key):
            raise SecretStoreError("keychain is locked")

        monkeypatch.setattr(KeyringSecretStore, "rename", _raise)
        with pytest.raises(SecretStoreError):
            service.update_person(
                PersonUpdateInput(
                    **{
                        **_person_input(config_dir).model_dump(),
                        "original_person_id": "alice",
                        "person_id": "alice-2",
                    }
                )
            )

        assert (config_dir / "team" / "members" / "alice" / "person.yml").exists()
        assert not (config_dir / "team" / "members" / "alice-2").exists()

    def test_write_person_store_failure_writes_no_member_config(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)

        def _raise(self, key, value):
            raise SecretStoreError("keychain is locked")

        monkeypatch.setattr(KeyringSecretStore, "set", _raise)
        with pytest.raises(SecretStoreError):
            SimplePersonSetupService().write_person(
                _person_input(config_dir), github_private_key="pem-content"
            )

        assert not (config_dir / "team" / "members" / "alice").exists()

    def test_delete_person_store_failure_keeps_member_config(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        config_dir = self._workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, github_access_token="ghp-old"))

        def _raise(self, key):
            raise SecretStoreError("keychain is locked")

        monkeypatch.setattr(KeyringSecretStore, "delete", _raise)
        with pytest.raises(SecretStoreError):
            service.delete_person(config_dir=config_dir, person_id="alice")

        assert (config_dir / "team" / "members" / "alice" / "person.yml").exists()

        monkeypatch.undo()
        service.delete_person(config_dir=config_dir, person_id="alice")
        assert not (config_dir / "team" / "members" / "alice").exists()
