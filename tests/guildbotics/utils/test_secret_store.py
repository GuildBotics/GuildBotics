from __future__ import annotations

import json
import shutil

import pytest

from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    dump_yaml,
    load_yaml_dict,
)
from guildbotics.utils.keychain import InvalidSecretKeyError, SecretStoreError
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

    store.confirm_shared({"ANTHROPIC_API_KEY": 1}, sent={"ANTHROPIC_API_KEY": "first"})

    state = store.key_state("ANTHROPIC_API_KEY")
    assert state.status is SecretKeyStatus.READY
    assert (state.shared_generation, state.local_generation) == (1, 1)
    assert state.pending_send is False


def test_confirming_a_send_after_the_value_changed_keeps_it_pending(
    fake_keyring, tmp_path, monkeypatch
):
    """The exchange with the hub is not inside this store's lock, so a value
    can be entered while a send is in flight. The generation the hub took is
    still published -- that much is true -- but this machine must not claim to
    hold it: the newer value has not been shared, and clearing the flag would
    lose the only record saying so."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("ANTHROPIC_API_KEY", "sent-to-hub")
    store.set("ANTHROPIC_API_KEY", "typed-in-between")

    store.confirm_shared(
        {"ANTHROPIC_API_KEY": 1}, sent={"ANTHROPIC_API_KEY": "sent-to-hub"}
    )

    state = store.key_state("ANTHROPIC_API_KEY")
    assert state.shared_generation == 1
    assert state.pending_send is True
    assert state.status is SecretKeyStatus.CONFLICT
    assert store.get("ANTHROPIC_API_KEY") == "typed-in-between"


def test_a_generation_for_a_key_never_offered_is_not_published(
    fake_keyring, tmp_path, monkeypatch
):
    """A hub answer naming a key this device did not send can only be corrupt;
    publishing it would tell every machine to fetch a value nobody here
    vouched for."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    store.confirm_shared({"OPENAI_API_KEY": 1}, sent={})

    assert store.shared_generation("OPENAI_API_KEY") is None


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
    # this device created is not in it.
    store.location.write_text(dump_yaml({"keys": {}}), encoding="utf-8")

    assert store.keys() == ["OPENAI_API_KEY"]
    assert store.key_state("OPENAI_API_KEY").status is SecretKeyStatus.PENDING_SEND
    assert store.get("OPENAI_API_KEY") == "sk-test"


def test_a_shared_generation_never_moves_backwards(fake_keyring, tmp_path, monkeypatch):
    """Two devices can each reach the hub and publish; the answer that comes
    back later must not put the earlier number over the later one."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "sk-test")
    store.confirm_shared({"OPENAI_API_KEY": 3}, sent={"OPENAI_API_KEY": "sk-test"})

    store.confirm_shared({"OPENAI_API_KEY": 2}, sent={"OPENAI_API_KEY": "sk-test"})

    assert store.shared_generation("OPENAI_API_KEY") == 3


def test_get_refuses_stale_generation(fake_keyring, tmp_path, monkeypatch):
    """When another device advanced the shared generation, the local keychain
    value is outdated and must not be served."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("OPENAI_API_KEY", "old-value")
    store.confirm_shared({"OPENAI_API_KEY": 1}, sent={"OPENAI_API_KEY": "old-value"})
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
    store.confirm_shared(
        {"ALICE_GITHUB_ACCESS_TOKEN": 1},
        sent={"ALICE_GITHUB_ACCESS_TOKEN": "ghp-secret"},
    )
    # Another device bumped the shared generation; this device is stale now.
    _publish_shared_generation(store, "ALICE_GITHUB_ACCESS_TOKEN", 2)
    assert store.get("ALICE_GITHUB_ACCESS_TOKEN") is None

    store.rename("ALICE_GITHUB_ACCESS_TOKEN", "ALICE_2_GITHUB_ACCESS_TOKEN")

    assert store.keys() == ["ALICE_2_GITHUB_ACCESS_TOKEN"]
    assert store.shared_generation("ALICE_2_GITHUB_ACCESS_TOKEN") == 2
    assert store.get("ALICE_2_GITHUB_ACCESS_TOKEN") is None
    assert "ALICE_2_GITHUB_ACCESS_TOKEN" in store.stale_keys()


def _local_record(workspace) -> dict:
    return json.loads(
        (workspace / ".guildbotics" / "local" / "secrets.json").read_text(
            encoding="utf-8"
        )
    )


def _namespaces(fake_keyring) -> set[str]:
    return {service for service, _ in fake_keyring.passwords}


def test_the_keychain_namespace_is_this_devices_own(
    fake_keyring, tmp_path, monkeypatch
):
    """The namespace is kept beside the generations this device holds, never in
    the index every device shares, and a new store -- a later process -- uses
    the same one."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    config_dir = tmp_path / ".guildbotics" / "config"
    resolve_secret_store(config_dir, create_default=True)
    store_id = _local_record(tmp_path)["store_id"]

    KeyringSecretStore(config_dir).set("OPENAI_API_KEY", "sk-test")

    index = load_yaml_dict(config_dir / "secrets.yml")
    assert list(index) == ["keys"]
    assert list(index["keys"]) == ["OPENAI_API_KEY"]
    assert _local_record(tmp_path)["store_id"] == store_id
    assert _namespaces(fake_keyring) == {f"GuildBotics/{store_id}"}
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "sk-test"


def test_every_operation_uses_this_devices_namespace(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("FIRST_TOKEN", "one")
    namespace = f"GuildBotics/{_local_record(tmp_path)['store_id']}"
    store.confirm_shared({"FIRST_TOKEN": 1}, sent={"FIRST_TOKEN": "one"})
    assert store.key_state("FIRST_TOKEN").status is SecretKeyStatus.READY
    store.set("SECOND_TOKEN", "two")
    store.rename("SECOND_TOKEN", "RENAMED_TOKEN")
    _publish_shared_generation(store, "FIRST_TOKEN", 2)
    store.adopt_received("FIRST_TOKEN", "fetched", 2)
    store.set("GONE_TOKEN", "three")
    store.delete("GONE_TOKEN")

    assert fake_keyring.passwords == {
        (namespace, "FIRST_TOKEN"): "fetched",
        (namespace, "RENAMED_TOKEN"): "two",
    }
    assert store.get("FIRST_TOKEN") == "fetched"
    assert store.get("RENAMED_TOKEN") == "two"


def test_adopting_another_devices_index_keeps_the_namespace(
    fake_keyring, tmp_path, monkeypatch
):
    """Joining a hub or synchronizing replaces the shared index wholesale; a
    value entered here and not yet sent stays readable."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("UNSENT_TOKEN", "typed-here")
    store_id = _local_record(tmp_path)["store_id"]

    store.location.write_text(
        dump_yaml({"keys": {"HUB_TOKEN": {"generation": 3}}}), encoding="utf-8"
    )

    assert store.get("UNSENT_TOKEN") == "typed-here"
    assert store.key_state("UNSENT_TOKEN").status is SecretKeyStatus.PENDING_SEND
    assert store.key_state("HUB_TOKEN").status is SecretKeyStatus.MISSING
    assert _local_record(tmp_path)["store_id"] == store_id


@pytest.mark.parametrize(
    "local_text",
    [
        json.dumps({"keys": {"OPENAI_API_KEY": {"generation": 1}}}),
        json.dumps({"store_id": "", "keys": {"OPENAI_API_KEY": {"generation": 1}}}),
        "{not json",
    ],
    ids=["no-namespace", "empty-namespace", "unparsable"],
)
def test_generations_without_their_namespace_start_over_empty(
    fake_keyring, tmp_path, monkeypatch, local_text
):
    """Generations describe values in the namespace recorded beside them. Kept
    without it, a key would read as held while nothing can be read, and a held
    key is never fetched -- so the record starts over, and every shared key is
    one to fetch from the hub."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    config_dir = tmp_path / ".guildbotics" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "secrets.yml").write_text(
        dump_yaml({"keys": {"OPENAI_API_KEY": {"generation": 1}}}), encoding="utf-8"
    )
    local = tmp_path / ".guildbotics" / "local" / "secrets.json"
    local.parent.mkdir(parents=True)
    local.write_text(local_text, encoding="utf-8")
    store = KeyringSecretStore(config_dir)

    assert store.key_state("OPENAI_API_KEY").status is SecretKeyStatus.MISSING
    assert store.get("OPENAI_API_KEY") is None

    store.adopt_received("OPENAI_API_KEY", "fetched", 1)

    store_id = _local_record(tmp_path)["store_id"]
    assert store_id
    assert _namespaces(fake_keyring) == {f"GuildBotics/{store_id}"}
    assert store.key_state("OPENAI_API_KEY").status is SecretKeyStatus.READY
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "fetched"


@pytest.mark.parametrize("relocate", [shutil.move, shutil.copytree])
def test_a_moved_or_copied_workspace_keeps_its_namespace(
    fake_keyring, tmp_path, monkeypatch, relocate
):
    """The namespace travels with ``local/``: a copy made on the same device
    shares it, so copying does not separate the secrets."""
    source = tmp_path / "source"
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(source))
    KeyringSecretStore(source / ".guildbotics" / "config").set("TOKEN", "value")
    store_id = _local_record(source)["store_id"]

    target = tmp_path / "target"
    relocate(str(source), str(target))
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(target))
    store = KeyringSecretStore(target / ".guildbotics" / "config")

    assert store.get("TOKEN") == "value"
    assert _local_record(target)["store_id"] == store_id


def test_no_secret_value_reaches_a_workspace_file(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value-that-must-stay-in-the-keychain")
    store.confirm_shared(
        {"TOKEN": 1}, sent={"TOKEN": "value-that-must-stay-in-the-keychain"}
    )
    store.adopt_received("OTHER_TOKEN", "fetched-value-that-must-stay", 1)

    for path in (tmp_path / ".guildbotics").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "must-stay" not in text, path


def test_a_key_only_the_local_record_names_can_be_deleted_and_renamed(
    fake_keyring, tmp_path, monkeypatch
):
    """After a join adopts an index without this device's entry, the key is
    still listed, so the operations on listed keys must reach it too."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("GONE_TOKEN", "one")
    store.set("OLD_TOKEN", "two")
    store.location.write_text(dump_yaml({"keys": {}}), encoding="utf-8")

    store.delete("GONE_TOKEN")
    store.rename("OLD_TOKEN", "NEW_TOKEN")

    assert store.keys() == ["NEW_TOKEN"]
    assert store.get("NEW_TOKEN") == "two"
    assert store.key_state("NEW_TOKEN").status is SecretKeyStatus.PENDING_SEND
    assert load_yaml_dict(store.location) == {"keys": {}}
    namespace = f"GuildBotics/{_local_record(tmp_path)['store_id']}"
    assert fake_keyring.passwords == {(namespace, "NEW_TOKEN"): "two"}


def test_initializing_again_keeps_the_namespace_and_what_it_holds(
    fake_keyring, tmp_path, monkeypatch
):
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    before = _local_record(tmp_path)

    store.ensure_initialized()

    assert _local_record(tmp_path) == before
    assert store.get("TOKEN") == "value"


@pytest.mark.parametrize(
    "operation",
    [
        lambda store: store.delete("UNKNOWN"),
        lambda store: store.rename("UNKNOWN", "NEW"),
    ],
    ids=["delete", "rename"],
)
def test_an_operation_on_an_unknown_key_records_no_namespace(
    fake_keyring, tmp_path, monkeypatch, operation
):
    """A namespace no value was written under is not one to keep."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    operation(store)

    assert not (tmp_path / ".guildbotics" / "local" / "secrets.json").exists()
    assert not store.location.exists()


def test_a_record_that_exists_but_cannot_be_read_is_an_error(
    fake_keyring, tmp_path, monkeypatch
):
    """Starting over would replace the namespace the record names on the next
    write, leaving its values where nothing records them."""
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(tmp_path))
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    (tmp_path / ".guildbotics" / "local" / "secrets.json").mkdir(parents=True)

    with pytest.raises(SecretStoreError):
        store.set("TOKEN", "value")

    assert fake_keyring.passwords == {}
