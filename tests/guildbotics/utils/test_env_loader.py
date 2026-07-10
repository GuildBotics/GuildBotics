import os

from guildbotics.utils.env_loader import (
    GUILDBOTICS_ENV_FILE,
    HOME_ENV_PROTECTED_KEYS,
    load_guildbotics_env,
    read_workspace_secrets,
)
from guildbotics.utils.secret_store import KeyringSecretStore


def test_load_guildbotics_env_prefers_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "secrets.env"
    cwd_env = tmp_path / ".env"
    env_file.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-file\n", encoding="utf-8")
    cwd_env.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-cwd\n", encoding="utf-8")
    monkeypatch.setenv(GUILDBOTICS_ENV_FILE, str(env_file))

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=True)

    assert loaded == env_file
    assert loaded is not None
    assert loaded.is_absolute()
    assert loaded.read_text(encoding="utf-8")
    assert loaded == env_file.resolve()
    assert os.environ["AIKO_GITHUB_ACCESS_TOKEN"] == "from-file"
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(env_file.resolve())


def test_load_guildbotics_env_sets_absolute_path(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("AIKO_GITHUB_ACCESS_TOKEN=from-cwd\n", encoding="utf-8")

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert loaded == env_file.resolve()
    assert loaded is not None
    assert loaded.is_absolute()
    assert loaded.read_text(encoding="utf-8")
    assert os.environ["AIKO_GITHUB_ACCESS_TOKEN"] == "from-cwd"
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(env_file.resolve())


def test_load_guildbotics_env_skips_home_keys_when_unset(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "HOME=workspace-home",
                "USERPROFILE=workspace-userprofile",
                "HOMEDRIVE=Z:",
                "HOMEPATH=\\Users\\Workspace",
                "WORKSPACE_MARKER=loaded",
            ]
        ),
        encoding="utf-8",
    )
    for key in HOME_ENV_PROTECTED_KEYS:
        monkeypatch.delenv(key, raising=False)

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert loaded == env_file.resolve()
    for key in HOME_ENV_PROTECTED_KEYS:
        assert key not in os.environ
    assert os.environ["WORKSPACE_MARKER"] == "loaded"
    assert os.environ[GUILDBOTICS_ENV_FILE] == str(env_file.resolve())

def _keyring_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    config_dir = tmp_path / ".guildbotics" / "config"
    config_dir.mkdir(parents=True)
    return KeyringSecretStore(config_dir)


def test_read_workspace_secrets_empty_for_legacy_workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-env-file\n", encoding="utf-8")

    assert read_workspace_secrets(tmp_path) == {}


def test_load_guildbotics_env_prefers_keychain_over_env_file(
    fake_keyring, tmp_path, monkeypatch
):
    store = _keyring_workspace(tmp_path, monkeypatch)
    store.set("KEYCHAIN_TEST_TOKEN", "from-keychain")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KEYCHAIN_TEST_TOKEN=from-env-file\nPLAIN_TEST_SETTING=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KEYCHAIN_TEST_TOKEN", raising=False)
    monkeypatch.delenv("PLAIN_TEST_SETTING", raising=False)

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert loaded == env_file.resolve()
    assert os.environ["KEYCHAIN_TEST_TOKEN"] == "from-keychain"
    assert os.environ["PLAIN_TEST_SETTING"] == "from-env-file"
    monkeypatch.delenv("KEYCHAIN_TEST_TOKEN")
    monkeypatch.delenv("PLAIN_TEST_SETTING")


def test_load_guildbotics_env_keeps_real_environment_variables(
    fake_keyring, tmp_path, monkeypatch
):
    store = _keyring_workspace(tmp_path, monkeypatch)
    store.set("KEYCHAIN_TEST_TOKEN", "from-keychain")
    monkeypatch.setenv("KEYCHAIN_TEST_TOKEN", "from-real-env")

    load_guildbotics_env(tmp_path, override=False, prefer_env_file=False)

    assert os.environ["KEYCHAIN_TEST_TOKEN"] == "from-real-env"


def test_load_guildbotics_env_loads_keychain_without_env_file(
    fake_keyring, tmp_path, monkeypatch
):
    store = _keyring_workspace(tmp_path, monkeypatch)
    store.set("KEYCHAIN_TEST_TOKEN", "from-keychain")
    monkeypatch.delenv("KEYCHAIN_TEST_TOKEN", raising=False)

    loaded = load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert loaded is None
    assert os.environ["KEYCHAIN_TEST_TOKEN"] == "from-keychain"
    monkeypatch.delenv("KEYCHAIN_TEST_TOKEN")
