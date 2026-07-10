"""Keychain-backed secret handling in the simple setup service.

The legacy ``.env`` behaviours are covered by ``test_setup_service*.py``;
these tests pin the workspace to the keyring backend (via the ``fake_keyring``
fixture) and assert that secrets go to the OS keychain while non-secret
values and sample settings stay in plain configuration files.
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
from guildbotics.utils.secret_store import (
    KEYRING_BACKEND,
    SECRETS_INDEX_FILENAME,
    KeyringSecretStore,
    configured_secrets_backend,
    read_env_values,
)


def _project_input(config_dir: Path, env_file_path: Path, **overrides):
    payload: dict = {
        "config_dir": config_dir,
        "env_file_path": env_file_path,
        "env_file_option": "overwrite",
        "language": "en",
        "llm_api_type": "openai",
        "cli_agent": "codex",
        "provider_api_keys": {"openai": "sk-secret"},
    }
    payload.update(overrides)
    return ProjectSetupInput(**payload)


def _person_input(config_dir: Path, env_file_path: Path, **overrides):
    payload: dict = {
        "config_dir": config_dir,
        "env_file_path": env_file_path,
        "append_env_file": False,
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


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".guildbotics" / "config", tmp_path / ".env"


class TestProjectSecrets:
    def test_write_project_stores_api_key_in_keychain(self, fake_keyring, tmp_path):
        config_dir, env_file = _paths(tmp_path)

        SimpleProjectSetupService().write_project(_project_input(config_dir, env_file))

        assert configured_secrets_backend(config_dir) == KEYRING_BACKEND
        assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "sk-secret"
        env_content = env_file.read_text()
        assert "sk-secret" not in env_content
        assert "OPENAI_API_KEY" not in env_content

    def test_write_project_without_key_still_pins_backend(
        self, fake_keyring, tmp_path
    ):
        config_dir, env_file = _paths(tmp_path)

        SimpleProjectSetupService().write_project(
            _project_input(config_dir, env_file, provider_api_keys={})
        )

        assert configured_secrets_backend(config_dir) == KEYRING_BACKEND
        assert (config_dir / SECRETS_INDEX_FILENAME).exists()

    def test_read_project_config_sees_keychain_keys(self, fake_keyring, tmp_path):
        config_dir, env_file = _paths(tmp_path)
        service = SimpleProjectSetupService()
        service.write_project(_project_input(config_dir, env_file))

        snapshot = service.read_project_config(
            config_dir=config_dir, env_file_path=env_file
        )

        assert snapshot.provider_api_keys["openai"] is True

    def test_update_project_stores_new_key_in_keychain(self, fake_keyring, tmp_path):
        config_dir, env_file = _paths(tmp_path)
        service = SimpleProjectSetupService()
        service.write_project(_project_input(config_dir, env_file))

        service.update_project(
            ProjectUpdateInput(
                config_dir=config_dir,
                env_file_path=env_file,
                language="en",
                llm_api_type="anthropic",
                provider_api_keys={"anthropic": "sk-ant-secret"},
            )
        )

        assert KeyringSecretStore(config_dir).get("ANTHROPIC_API_KEY") == (
            "sk-ant-secret"
        )
        assert "sk-ant-secret" not in env_file.read_text()

    def test_update_project_keeps_legacy_env_file_workspace(
        self, fake_keyring, tmp_path
    ):
        # A pre-existing workspace without a secrets index stays on .env even
        # when a keychain is available; only setup/migrate switches backends.
        config_dir, env_file = _paths(tmp_path)
        config_dir.mkdir(parents=True)
        env_file.write_text("OPENAI_API_KEY=sk-old\n")

        SimpleProjectSetupService().update_project(
            ProjectUpdateInput(
                config_dir=config_dir,
                env_file_path=env_file,
                language="en",
                provider_api_keys={"openai": "sk-new"},
            )
        )

        assert configured_secrets_backend(config_dir) is None
        assert read_env_values(env_file)["OPENAI_API_KEY"] == "sk-new"


class TestPersonSecrets:
    def _pinned_workspace(self, tmp_path: Path) -> tuple[Path, Path]:
        config_dir, env_file = _paths(tmp_path)
        config_dir.mkdir(parents=True)
        env_file.write_text("")
        KeyringSecretStore(config_dir).ensure_initialized()
        return config_dir, env_file

    def test_write_person_stores_tokens_in_keychain(self, fake_keyring, tmp_path):
        config_dir, env_file = self._pinned_workspace(tmp_path)

        result = SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                env_file,
                append_env_file=True,
                github_access_token="ghp-secret",
                slack_bot_token="xoxb-secret",
                github_installation_id=42,
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-secret"
        assert store.get("ALICE_SLACK_BOT_TOKEN") == "xoxb-secret"
        env_content = env_file.read_text()
        assert "ghp-secret" not in env_content
        assert "xoxb-secret" not in env_content
        assert read_env_values(env_file)["ALICE_GITHUB_INSTALLATION_ID"] == "42"
        assert "ALICE_GITHUB_ACCESS_TOKEN=********" in (
            result.masked_environment_variables
        )

    def test_read_person_config_sees_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, env_file, github_access_token="ghp-secret")
        )

        snapshot = service.read_person_config(
            config_dir=config_dir, person_id="alice", env_file_path=env_file
        )

        assert snapshot.has_github_access_token is True
        assert snapshot.has_slack_bot_token is False

    def test_update_person_rename_moves_keychain_tokens(self, fake_keyring, tmp_path):
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, env_file, github_access_token="ghp-secret")
        )

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir, env_file).model_dump(),
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
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(config_dir, env_file, github_access_token="ghp-secret")
        )

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir, env_file).model_dump(),
                    "original_person_id": "alice",
                    "github_access_token": "",
                }
            )
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_ACCESS_TOKEN") == (
            "ghp-secret"
        )

    def test_update_person_migrates_legacy_env_secret(self, fake_keyring, tmp_path):
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, env_file))
        env_file.write_text("ALICE_GITHUB_ACCESS_TOKEN=ghp-legacy\n")

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir, env_file).model_dump(),
                    "original_person_id": "alice",
                }
            )
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_ACCESS_TOKEN") == (
            "ghp-legacy"
        )
        assert "ghp-legacy" not in env_file.read_text()

    def test_write_person_copies_private_key_content_to_keychain(
        self, fake_keyring, tmp_path
    ):
        config_dir, env_file = self._pinned_workspace(tmp_path)
        pem_file = tmp_path / "alice.pem"
        pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\npem\n")

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                env_file,
                append_env_file=True,
                github_private_key_path=pem_file,
                github_app_id=7,
            )
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_PRIVATE_KEY") == pem_file.read_text()
        # The keychain content replaces the file: no path entry lands in
        # .env, and only the file deletion is left to the user.
        assert "ALICE_GITHUB_PRIVATE_KEY_PATH" not in read_env_values(env_file)
        assert read_env_values(env_file)["ALICE_GITHUB_APP_ID"] == "7"
        assert pem_file.exists()

        snapshot = SimplePersonSetupService().read_person_config(
            config_dir=config_dir, person_id="alice", env_file_path=env_file
        )
        assert snapshot.has_github_private_key is True
        assert snapshot.has_github_private_key_path is False

    def test_write_person_ignores_unreadable_private_key_path(
        self, fake_keyring, tmp_path
    ):
        config_dir, env_file = self._pinned_workspace(tmp_path)

        SimplePersonSetupService().write_person(
            _person_input(
                config_dir,
                env_file,
                github_private_key_path=tmp_path / "missing.pem",
            )
        )

        assert KeyringSecretStore(config_dir).get("ALICE_GITHUB_PRIVATE_KEY") is None

    def test_update_person_rename_moves_private_key_content(
        self, fake_keyring, tmp_path
    ):
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(_person_input(config_dir, env_file))
        KeyringSecretStore(config_dir).set("ALICE_GITHUB_PRIVATE_KEY", "pem-content")

        service.update_person(
            PersonUpdateInput(
                **{
                    **_person_input(config_dir, env_file).model_dump(),
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
        config_dir, env_file = self._pinned_workspace(tmp_path)
        service = SimplePersonSetupService()
        service.write_person(
            _person_input(
                config_dir,
                env_file,
                github_access_token="ghp-secret",
                slack_app_token="xapp-secret",
            )
        )

        service.delete_person(
            config_dir=config_dir, person_id="alice", env_file_path=env_file
        )

        store = KeyringSecretStore(config_dir)
        assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None
        assert store.get("ALICE_SLACK_APP_TOKEN") is None
        assert store.keys() == []
