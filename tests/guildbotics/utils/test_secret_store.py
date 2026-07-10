import os
import stat
import sys

import pytest

from guildbotics.utils.fileio import load_yaml_dict
from guildbotics.utils.secret_store import (
    ENV_FILE_BACKEND,
    KEYRING_BACKEND,
    SECRETS_BACKEND_ENV,
    SECRETS_INDEX_FILENAME,
    EnvFileSecretStore,
    KeyringSecretStore,
    configured_secrets_backend,
    format_env_line,
    keyring_available,
    read_env_values,
    resolve_secret_store,
    write_env_values,
)

PEM_VALUE = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    'MIIEow+abc/123 "quoted" back\\slash\n'
    "-----END RSA PRIVATE KEY-----\n"
)


class TestEnvFileSecretStore:
    def test_set_get_delete_roundtrip(self, tmp_path):
        store = EnvFileSecretStore(tmp_path / ".env")

        store.set("OPENAI_API_KEY", "sk-test")
        store.set("ALICE_SLACK_BOT_TOKEN", "xoxb-1")

        assert store.get("OPENAI_API_KEY") == "sk-test"
        assert store.keys() == ["OPENAI_API_KEY", "ALICE_SLACK_BOT_TOKEN"]
        assert store.values() == {
            "OPENAI_API_KEY": "sk-test",
            "ALICE_SLACK_BOT_TOKEN": "xoxb-1",
        }

        store.delete("OPENAI_API_KEY")
        assert store.get("OPENAI_API_KEY") is None
        assert store.keys() == ["ALICE_SLACK_BOT_TOKEN"]

    def test_preserves_unrelated_env_entries(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("LOG_LEVEL=debug\n")
        store = EnvFileSecretStore(env_file)

        store.set("OPENAI_API_KEY", "sk-test")

        assert read_env_values(env_file) == {
            "LOG_LEVEL": "debug",
            "OPENAI_API_KEY": "sk-test",
        }

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_written_env_file_is_owner_only(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_values(env_file, {"OPENAI_API_KEY": "sk-test"})

        mode = stat.S_IMODE(os.stat(env_file).st_mode)
        assert mode == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_write_tightens_world_readable_file_and_leaves_no_temp(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("OLD=1\n")
        env_file.chmod(0o644)

        write_env_values(env_file, {"OPENAI_API_KEY": "sk-test"})

        assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
        assert [path.name for path in tmp_path.iterdir()] == [".env"]

    def test_multiline_value_survives_write_read_roundtrip(self, tmp_path):
        env_file = tmp_path / ".env"
        write_env_values(env_file, {"PEM": PEM_VALUE, "PLAIN": "sk-test"})

        assert read_env_values(env_file) == {"PEM": PEM_VALUE, "PLAIN": "sk-test"}

    def test_store_roundtrips_multiline_value(self, tmp_path):
        store = EnvFileSecretStore(tmp_path / ".env")
        store.set("PLAIN", "sk-test")
        store.set("PEM", PEM_VALUE)

        assert store.get("PEM") == PEM_VALUE
        assert store.get("PLAIN") == "sk-test"


class TestFormatEnvLine:
    def test_plain_value_stays_unquoted(self):
        assert format_env_line("KEY", "sk-test_1.2/3+=") == "KEY=sk-test_1.2/3+="

    @pytest.mark.parametrize(
        "value",
        ["two words", 'with "quote"', "with#hash", "back\\slash", "multi\nline\r"],
    )
    def test_special_values_are_quoted_and_roundtrip(self, tmp_path, value):
        line = format_env_line("KEY", value)
        assert line.startswith('KEY="')
        env_file = tmp_path / ".env"
        env_file.write_text(line + "\nOTHER=x\n")
        values = read_env_values(env_file)
        assert values == {"KEY": value, "OTHER": "x"}


class TestKeyringSecretStore:
    def test_set_get_delete_roundtrip(self, fake_keyring, tmp_path):
        store = KeyringSecretStore(tmp_path)

        store.set("OPENAI_API_KEY", "sk-test")
        assert store.get("OPENAI_API_KEY") == "sk-test"
        assert store.keys() == ["OPENAI_API_KEY"]

        store.delete("OPENAI_API_KEY")
        assert store.get("OPENAI_API_KEY") is None
        assert store.keys() == []

    def test_index_file_names_keys_without_values(self, fake_keyring, tmp_path):
        store = KeyringSecretStore(tmp_path)
        store.set("B_KEY", "value-b")
        store.set("A_KEY", "value-a")

        index = load_yaml_dict(tmp_path / SECRETS_INDEX_FILENAME)
        assert index["backend"] == KEYRING_BACKEND
        assert index["keys"] == ["A_KEY", "B_KEY"]
        content = (tmp_path / SECRETS_INDEX_FILENAME).read_text()
        assert "value-a" not in content
        assert "value-b" not in content

    def test_store_id_is_stable_across_instances(self, fake_keyring, tmp_path):
        KeyringSecretStore(tmp_path).set("A_KEY", "value-a")
        store_id = load_yaml_dict(tmp_path / SECRETS_INDEX_FILENAME)["store_id"]

        KeyringSecretStore(tmp_path).set("B_KEY", "value-b")
        assert load_yaml_dict(tmp_path / SECRETS_INDEX_FILENAME)["store_id"] == store_id
        assert KeyringSecretStore(tmp_path).get("A_KEY") == "value-a"

    def test_delete_missing_key_is_noop(self, fake_keyring, tmp_path):
        KeyringSecretStore(tmp_path).delete("MISSING")

    def test_ensure_initialized_pins_backend(self, fake_keyring, tmp_path):
        KeyringSecretStore(tmp_path).ensure_initialized()

        assert configured_secrets_backend(tmp_path) == KEYRING_BACKEND
        assert load_yaml_dict(tmp_path / SECRETS_INDEX_FILENAME)["keys"] == []


class TestBackendResolution:
    def test_env_override_wins_over_index_file(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        KeyringSecretStore(tmp_path).ensure_initialized()
        monkeypatch.setenv(SECRETS_BACKEND_ENV, ENV_FILE_BACKEND)

        assert configured_secrets_backend(tmp_path) == ENV_FILE_BACKEND
        store = resolve_secret_store(tmp_path, tmp_path / ".env")
        assert isinstance(store, EnvFileSecretStore)

    def test_unconfigured_workspace_defaults_to_env_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv(SECRETS_BACKEND_ENV, raising=False)

        assert configured_secrets_backend(tmp_path) is None
        store = resolve_secret_store(tmp_path, tmp_path / ".env")
        assert isinstance(store, EnvFileSecretStore)

    def test_create_default_prefers_keyring_when_available(
        self, fake_keyring, tmp_path
    ):
        assert keyring_available()
        store = resolve_secret_store(
            tmp_path, tmp_path / ".env", create_default=True
        )
        assert isinstance(store, KeyringSecretStore)

    def test_index_file_pins_keyring_backend(self, fake_keyring, tmp_path):
        KeyringSecretStore(tmp_path).ensure_initialized()

        store = resolve_secret_store(tmp_path, tmp_path / ".env")
        assert isinstance(store, KeyringSecretStore)
