from __future__ import annotations

import json

import pytest

from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    dump_yaml,
    load_yaml_dict,
)
from guildbotics.utils.keychain import InvalidSecretKeyError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    SecretKeyStatus,
    format_env_line,
    is_secret_env_key,
    keyring_available,
    keyring_status,
    known_secret_env_keys,
    read_env_values,
    register_secret_env_keys,
    resolve_secret_store,
    write_env_values,
)


def test_is_secret_env_key_matches_by_name_or_provenance():
    assert is_secret_env_key("AIKO_GITHUB_ACCESS_TOKEN")
    assert is_secret_env_key("PGPASSWORD")
    assert not is_secret_env_key("DATABASE_URL")

    register_secret_env_keys(["DATABASE_URL"])

    assert is_secret_env_key("DATABASE_URL")
    assert "DATABASE_URL" in known_secret_env_keys()
    # The registry only grows within a process; repeated loads union.
    register_secret_env_keys(["DOCKER_AUTH_CONFIG"])
    assert known_secret_env_keys() >= {"DATABASE_URL", "DOCKER_AUTH_CONFIG"}


def test_keyring_store_writes_key_index_without_sharing_a_generation(
    fake_keyring, tmp_path, monkeypatch
):
    """A value typed in here is usable at once and waiting to be sent.

    The shared generation stays where it was: it names a value every device can
    fetch from the hub, and the hub has not been given this one."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    config_dir = tmp_path / ".guildbotics" / "config"
    store = KeyringSecretStore(config_dir)
    store.set("OPENAI_API_KEY", "sk-test")

    index = store.location.read_text(encoding="utf-8")
    assert "backend:" not in index
    assert "OPENAI_API_KEY:" in index
    assert "generation: 0" in index
    assert store.get("OPENAI_API_KEY") == "sk-test"
    assert store.shared_generation("OPENAI_API_KEY") == 0
    assert store.local_generation("OPENAI_API_KEY") == 0
    state = store.key_state("OPENAI_API_KEY")
    assert state is not None
    assert state.status is SecretKeyStatus.PENDING_SEND


def test_repeated_local_updates_stay_one_unsent_update(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ANTHROPIC_API_KEY", "first")
    store.set("ANTHROPIC_API_KEY", "second")

    assert store.shared_generation("ANTHROPIC_API_KEY") == 0
    assert store.get("ANTHROPIC_API_KEY") == "second"
    local = json.loads(
        (tmp_path / ".guildbotics" / "local" / "secrets.json").read_text(
            encoding="utf-8"
        )
    )
    assert local["keys"]["ANTHROPIC_API_KEY"]["generation"] == 0
    assert local["keys"]["ANTHROPIC_API_KEY"]["pending_send"] is True


def test_confirming_a_send_publishes_the_generation(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ANTHROPIC_API_KEY", "first")
    assert store.key_state("ANTHROPIC_API_KEY").status is SecretKeyStatus.PENDING_SEND

    store.confirm_shared({"ANTHROPIC_API_KEY": 1})

    state = store.key_state("ANTHROPIC_API_KEY")
    assert state.status is SecretKeyStatus.READY
    assert (state.shared_generation, state.local_generation) == (1, 1)
    assert state.pending_send is False


def test_a_local_update_against_a_newer_shared_generation_conflicts(
    fake_keyring, tmp_path, monkeypatch
):
    """Two machines changed one key: this one still serves its own value, and
    says so, rather than silently overwriting or discarding either side."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "typed-here")
    _publish_shared_generation(store, "OPENAI_API_KEY", 1)

    state = store.key_state("OPENAI_API_KEY")
    assert state.status is SecretKeyStatus.CONFLICT
    assert store.get("OPENAI_API_KEY") == "typed-here"
    assert store.stale_keys() == []


def _publish_shared_generation(store, key: str, generation: int) -> None:
    """Raise the shared generation the way synchronization does.

    Another device's update arrives as a checkout of the index file, not
    through a writer, so the file is written directly here too."""
    index = load_yaml_dict(store.location)
    index["keys"][key]["generation"] = generation
    store.location.write_text(dump_yaml(index), encoding="utf-8")


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


def test_a_key_name_that_could_not_be_transferred_is_refused(
    fake_keyring, tmp_path, monkeypatch
):
    """A key becomes an environment variable on every device and an argument to
    the hub's own command on another machine, so one that could be neither is
    refused where it would be created rather than where it would be used."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    for name in ("", "with space", "../escape", "a-dash", "semi;colon", 'quo"te'):
        with pytest.raises(InvalidSecretKeyError):
            store.set(name, "value")
    assert store.keys() == []


def test_a_member_named_with_a_leading_digit_still_has_keys(
    fake_keyring, tmp_path, monkeypatch
):
    """Member keys are named after the member, and a person_id may start with a
    digit. What a name has to survive is a shell, not an identifier parser."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    store.set("2B_GITHUB_ACCESS_TOKEN", "ghp-secret")

    assert store.get("2B_GITHUB_ACCESS_TOKEN") == "ghp-secret"


def test_a_key_only_the_local_record_names_is_still_a_key(
    fake_keyring, tmp_path, monkeypatch
):
    """The shared index can lose an entry -- a first-committer-wins race can set
    aside the very commit that created it -- and the value would then be held
    here under a name nothing lists."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "sk-test")
    # Synchronization delivers the hub's index as a checkout, and the entry
    # this device created is not in it. The workspace's keychain namespace is
    # part of that file and is unchanged.
    index = load_yaml_dict(store.location)
    index["keys"] = {}
    store.location.write_text(dump_yaml(index), encoding="utf-8")

    assert store.keys() == ["OPENAI_API_KEY"]
    assert store.key_state("OPENAI_API_KEY").status is SecretKeyStatus.PENDING_SEND
    assert store.get("OPENAI_API_KEY") == "sk-test"


def test_a_shared_generation_never_moves_backwards(fake_keyring, tmp_path, monkeypatch):
    """Two devices can each reach the hub and publish; the answer that comes
    back later must not put the earlier number over the later one."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "sk-test")
    store.confirm_shared({"OPENAI_API_KEY": 3})

    store.confirm_shared({"OPENAI_API_KEY": 2})

    assert store.shared_generation("OPENAI_API_KEY") == 3


def test_get_refuses_stale_generation(fake_keyring, tmp_path, monkeypatch):
    """When another device advanced the shared generation, the local keychain
    value is outdated and must not be served."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "old-value")
    store.confirm_shared({"OPENAI_API_KEY": 1})
    assert store.get("OPENAI_API_KEY") == "old-value"

    _publish_shared_generation(store, "OPENAI_API_KEY", 2)

    assert store.get("OPENAI_API_KEY") is None
    assert store.stale_keys() == ["OPENAI_API_KEY"]

    # Fetching the newer value from the hub realigns the device.
    store.adopt_received("OPENAI_API_KEY", "new-value", 2)
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
    assert store.shared_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 0
    assert store.local_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 0
    assert store.local_generation("ALICE_GITHUB_ACCESS_TOKEN") is None


def test_keyring_store_rename_moves_stale_metadata_and_keeps_it_stale(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ALICE_GITHUB_ACCESS_TOKEN", "ghp-secret")
    store.confirm_shared({"ALICE_GITHUB_ACCESS_TOKEN": 1})
    # Another device bumped the shared generation; this device is stale now.
    _publish_shared_generation(store, "ALICE_GITHUB_ACCESS_TOKEN", 2)
    assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None

    store.rename("ALICE_GITHUB_ACCESS_TOKEN", "ALICE_2_GITHUB_ACCESS_TOKEN")

    assert store.keys() == ["ALICE_2_GITHUB_ACCESS_TOKEN"]
    assert store.shared_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 2
    assert store.get("ALICE_2_GITHUB_ACCESS_TOKEN") is None
    assert "ALICE_2_GITHUB_ACCESS_TOKEN" in store.stale_keys()
