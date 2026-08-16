from __future__ import annotations

import json

from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    dump_yaml,
    load_yaml_dict,
)
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    format_env_line,
    keyring_available,
    keyring_status,
    read_env_values,
    resolve_secret_store,
    write_env_values,
)


def test_keyring_store_writes_generation_index(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    config_dir = tmp_path / ".guildbotics" / "config"
    store = KeyringSecretStore(config_dir)
    store.set("OPENAI_API_KEY", "sk-test")

    index = store.location.read_text(encoding="utf-8")
    assert "backend:" not in index
    assert "OPENAI_API_KEY:" in index
    assert "generation: 1" in index
    assert store.get("OPENAI_API_KEY") == "sk-test"
    assert store.shared_generation("OPENAI_API_KEY") == 1
    assert store.local_generation("OPENAI_API_KEY") == 1


def test_keyring_store_increments_generation_on_update(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ANTHROPIC_API_KEY", "first")
    store.set("ANTHROPIC_API_KEY", "second")

    assert store.shared_generation("ANTHROPIC_API_KEY") == 2
    assert store.get("ANTHROPIC_API_KEY") == "second"
    local = json.loads(
        (tmp_path / ".guildbotics" / "local" / "secrets.json").read_text(
            encoding="utf-8"
        )
    )
    assert local["keys"]["ANTHROPIC_API_KEY"]["generation"] == 2
    assert local["keys"]["ANTHROPIC_API_KEY"]["pending_send"] is False


def test_resolve_secret_store_uses_os_keychain(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = resolve_secret_store(
        tmp_path / ".guildbotics" / "config", create_default=True
    )
    assert isinstance(store, KeyringSecretStore)
    assert keyring_available() is True
    status = keyring_status()
    assert status["available"] is True
    assert status["locked"] is False


def test_dotenv_serializer_roundtrip_is_exchange_only(tmp_path):
    env_file = tmp_path / "export.env"
    write_env_values(env_file, {"KEY": "value with space", "OTHER": "plain"})
    assert read_env_values(env_file)["KEY"] == "value with space"
    assert format_env_line("PLAIN", "abc") == "PLAIN=abc"


def test_keyring_status_reports_reachable_store(fake_keyring):
    status = keyring_status()

    assert status == {
        "available": True,
        "locked": False,
        "backend": "os-keychain",
    }


def test_keyring_status_detects_locked_store(fake_keyring, monkeypatch):
    from keyring.errors import KeyringLocked

    class LockedKeychain:
        def get_password(self, service, username):
            raise KeyringLocked("collection is locked")

    monkeypatch.setattr(
        "guildbotics.utils.secret_store.system_keychain", lambda: LockedKeychain()
    )

    status = keyring_status()

    assert status["available"] is False
    assert status["locked"] is True
    assert status["backend"] == "os-keychain"


def test_keyring_status_detects_unreachable_store(fake_keyring, monkeypatch):
    class BrokenKeychain:
        def get_password(self, service, username):
            raise RuntimeError("no connection to the secret service")

    monkeypatch.setattr(
        "guildbotics.utils.secret_store.system_keychain", lambda: BrokenKeychain()
    )

    status = keyring_status()

    assert status["available"] is False
    assert status["locked"] is False


def test_get_refuses_stale_generation(fake_keyring, tmp_path, monkeypatch):
    """When another device advanced the shared generation, the local keychain
    value is outdated and must not be served."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "old-value")
    assert store.get("OPENAI_API_KEY") == "old-value"

    # Simulate a sync that raised the shared generation to 2. Synchronization
    # delivers the file as a checkout, not through a writer, so write directly.
    index_file = tmp_path / ".guildbotics" / "config" / "secrets.yml"
    data = load_yaml_dict(index_file)
    data["keys"]["OPENAI_API_KEY"]["generation"] = 2
    index_file.write_text(dump_yaml(data), encoding="utf-8")

    assert store.get("OPENAI_API_KEY") is None
    assert store.stale_keys() == ["OPENAI_API_KEY"]

    # A fresh local set realigns the device and serves the new value again.
    store.set("OPENAI_API_KEY", "new-value")
    assert store.get("OPENAI_API_KEY") == "new-value"
    assert store.stale_keys() == []


def test_store_anchors_local_index_to_its_config_dir(
    fake_keyring, tmp_path, monkeypatch
):
    """A store built for workspace B keeps ALL of its files in workspace B,
    even while workspace A is the selected one; switching to B later must
    serve the secret that was just stored."""
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace_a))

    KeyringSecretStore(workspace_b / ".guildbotics" / "config").set(
        "OPENAI_API_KEY", "secret-b"
    )

    assert (workspace_b / ".guildbotics" / "local" / "secrets.json").is_file()
    assert not (workspace_a / ".guildbotics" / "local" / "secrets.json").exists()

    # Now select workspace B and rebuild the store the way the runtime does.
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(workspace_b))
    store = KeyringSecretStore(workspace_b / ".guildbotics" / "config")
    assert store.get("OPENAI_API_KEY") == "secret-b"
    assert store.stale_keys() == []


def test_keyring_store_rename_moves_value_and_generations(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ALICE_GITHUB_ACCESS_TOKEN", "ghp-secret")

    store.rename("ALICE_GITHUB_ACCESS_TOKEN", "ALICE_2_GITHUB_ACCESS_TOKEN")

    assert store.keys() == ["ALICE_2_GITHUB_ACCESS_TOKEN"]
    assert store.get("ALICE_2_GITHUB_ACCESS_TOKEN") == "ghp-secret"
    assert store.shared_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 1
    assert store.local_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 1
    assert store.local_generation("ALICE_GITHUB_ACCESS_TOKEN") is None


def test_keyring_store_rename_moves_stale_metadata_and_keeps_it_stale(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ALICE_GITHUB_ACCESS_TOKEN", "ghp-secret")
    # Another device bumped the shared generation; this device is stale now.
    # Synchronization delivers the file as a checkout, so write it directly.
    index = load_yaml_dict(store.location)
    index["keys"]["ALICE_GITHUB_ACCESS_TOKEN"]["generation"] = 2
    store.location.write_text(dump_yaml(index), encoding="utf-8")
    assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None

    store.rename("ALICE_GITHUB_ACCESS_TOKEN", "ALICE_2_GITHUB_ACCESS_TOKEN")

    assert store.keys() == ["ALICE_2_GITHUB_ACCESS_TOKEN"]
    assert store.shared_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 2
    assert store.get("ALICE_2_GITHUB_ACCESS_TOKEN") is None
    assert "ALICE_2_GITHUB_ACCESS_TOKEN" in store.stale_keys()
