"""Keychain-backed secret handling in the simple setup service.

These tests pin the workspace to the OS keychain (via the ``fake_keyring``
fixture) and assert that secrets go there while non-secret values stay in
plain configuration files.
"""

from pathlib import Path

from guildbotics.editions.simple.setup_service import (
    PersonSetupInput,
    PersonUpdateInput,
    ProjectSetupInput,
    ProjectUpdateInput,
    SimplePersonSetupService,
    SimpleProjectSetupService,
)
from guildbotics.utils.fileio import load_yaml_file
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

    def test_update_person_rename_moves_keychain_tokens(self, fake_keyring, tmp_path):
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
                    "person_id": "alice-2",
                    "person_name": "Alice 2",
                }
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_2_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None

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

    def test_write_person_deletes_registration_generated_key_file(
        self, fake_keyring, tmp_path
    ):
        from guildbotics.editions.simple.setup_service import github_app_key_dir

        config_dir = self._workspace(tmp_path)
        generated_dir = github_app_key_dir()
        generated_dir.mkdir(parents=True, exist_ok=True)
        pem_file = generated_dir / "alice.private-key.pem"
        pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\npem\n")

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                github_private_key_path=pem_file,
                github_app_id=7,
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_PRIVATE_KEY") == (
            "-----BEGIN RSA PRIVATE KEY-----\npem\n"
        )
        assert not pem_file.exists()

    def test_write_person_ignores_unreadable_private_key_path(
        self, fake_keyring, tmp_path
    ):
        config_dir = self._workspace(tmp_path)

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                github_private_key_path=tmp_path / "missing.pem",
            )
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_PRIVATE_KEY") is None

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
