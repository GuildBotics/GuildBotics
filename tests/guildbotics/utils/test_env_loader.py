from __future__ import annotations

import os

from guildbotics.utils.env_loader import (
    ALLOWED_DEBUG_KEYS,
    load_guildbotics_env,
    read_debug_env,
    write_debug_env,
)
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    is_secret_env_key,
    known_secret_env_keys,
)


def test_load_guildbotics_env_uses_keychain_not_dotenv(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-dotenv\n", encoding="utf-8")
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "from-keychain")

    load_guildbotics_env(override=True)

    assert os.environ["OPENAI_API_KEY"] == "from-keychain"


def test_debug_env_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    write_debug_env({"LOG_LEVEL": "DEBUG", "AGNO_DEBUG": "true", "SECRET": "nope"})
    values = read_debug_env()
    assert values == {"LOG_LEVEL": "DEBUG", "AGNO_DEBUG": "true"}
    assert "SECRET" not in values
    assert ALLOWED_DEBUG_KEYS == {"LOG_LEVEL", "AGNO_DEBUG"}


def test_env_provided_secrets_are_not_read_from_the_keychain(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "from-keychain")
    store.set("SLACK_SIGNING", "from-keychain")
    monkeypatch.setenv("OPENAI_API_KEY", "from-real-env")
    monkeypatch.delenv("SLACK_SIGNING", raising=False)
    fetched: list[str] = []
    original_get = KeyringSecretStore.get

    def tracking_get(self, key):
        fetched.append(key)
        return original_get(self, key)

    monkeypatch.setattr(KeyringSecretStore, "get", tracking_get)

    load_guildbotics_env(override=False)

    assert os.environ["OPENAI_API_KEY"] == "from-real-env"
    assert os.environ.pop("SLACK_SIGNING") == "from-keychain"
    assert "OPENAI_API_KEY" not in fetched


def test_stored_keys_without_marker_names_are_registered_as_secrets(
    fake_keyring, tmp_path, monkeypatch
):
    """`secrets set` accepts any key name, so classification must follow the
    fact that a key is stored, not its spelling."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("DATABASE_URL", "postgres://user:hunter2@db/example")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not is_secret_env_key("DATABASE_URL")

    load_guildbotics_env(override=False)

    assert os.environ.pop("DATABASE_URL") == "postgres://user:hunter2@db/example"
    assert is_secret_env_key("DATABASE_URL")
    assert "DATABASE_URL" in known_secret_env_keys()


def test_env_provided_stored_keys_are_still_registered_as_secrets(
    fake_keyring, tmp_path, monkeypatch
):
    """Registration happens before the skip decision: a same-named real
    environment variable holds the secret value, so the key stays secret."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("DATABASE_URL", "postgres://user:hunter2@db/example")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:hunter2@db/other")

    load_guildbotics_env(override=False)

    assert os.environ["DATABASE_URL"] == "postgres://user:hunter2@db/other"
    assert is_secret_env_key("DATABASE_URL")


def test_locked_keychain_still_registers_stored_key_names(
    fake_keyring, tmp_path, monkeypatch
):
    """Registration happens before any fetch: a keychain failure loses the
    values, never the knowledge of which keys are secret."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("DATABASE_URL", "postgres://user:hunter2@db/example")

    def locked_get(self, key):
        raise SecretStoreError("keychain is locked")

    monkeypatch.setattr(KeyringSecretStore, "get", locked_get)

    load_guildbotics_env(override=True)

    assert is_secret_env_key("DATABASE_URL")


def test_locked_keychain_does_not_break_env_var_only_operation(
    fake_keyring, tmp_path, monkeypatch
):
    """Servers that provide every secret as a real environment variable must
    start even when the OS secret store is locked or unreachable."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "from-keychain")

    def locked_get(self, key):
        raise SecretStoreError("keychain is locked")

    monkeypatch.setattr(KeyringSecretStore, "get", locked_get)
    monkeypatch.setenv("OPENAI_API_KEY", "from-real-env")

    load_guildbotics_env(override=True)

    assert os.environ["OPENAI_API_KEY"] == "from-real-env"
