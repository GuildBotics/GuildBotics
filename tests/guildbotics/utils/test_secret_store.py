from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from guildbotics.utils.fileio import (
    GUILDBOTICS_WORKSPACE_ROOT,
    dump_yaml,
    load_yaml_dict,
)
from guildbotics.utils.keychain import InvalidSecretKeyError, SecretStoreError
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
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


def test_resolve_secret_store_uses_os_keychain(fake_keyring, tmp_path):
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


def test_a_key_name_that_cannot_be_an_environment_variable_is_refused(
    fake_keyring, tmp_path
):
    """A key becomes an environment variable, so one that could not be is
    refused where it would be created rather than where it would be used."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    for name in ("", "with space", "../escape", "a-dash", "semi;colon", 'quo"te'):
        with pytest.raises(InvalidSecretKeyError):
            store.set(name, "value")
    assert store.keys() == []


def test_a_member_named_with_a_leading_digit_still_has_keys(fake_keyring, tmp_path):
    """Member keys are named after the member, and a person_id may start with a
    digit."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    store.set("2B_GITHUB_ACCESS_TOKEN", "ghp-secret")

    assert store.get("2B_GITHUB_ACCESS_TOKEN") == "ghp-secret"


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


def _local_record(workspace) -> dict:
    return json.loads(
        (workspace / ".guildbotics" / "local" / "secrets.json").read_text(
            encoding="utf-8"
        )
    )


def _namespaces(fake_keyring) -> set[str]:
    return {service for service, _ in fake_keyring.passwords}


def test_the_keychain_namespace_is_this_devices_own(fake_keyring, tmp_path):
    """The namespace is kept in ``local/``, never in the index, and a new store
    -- a later process -- uses the same one."""
    config_dir = tmp_path / ".guildbotics" / "config"
    resolve_secret_store(config_dir, create_default=True)
    store_id = _local_record(tmp_path)["store_id"]

    KeyringSecretStore(config_dir).set("OPENAI_API_KEY", "sk-test")

    index = load_yaml_dict(config_dir / "secrets.yml")
    assert index == {"keys": ["OPENAI_API_KEY"]}
    assert _local_record(tmp_path)["store_id"] == store_id
    assert _namespaces(fake_keyring) == {f"GuildBotics/{store_id}"}
    assert KeyringSecretStore(config_dir).get("OPENAI_API_KEY") == "sk-test"


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


def test_initializing_again_keeps_the_namespace_and_what_it_holds(
    fake_keyring, tmp_path
):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    before = _local_record(tmp_path)

    store.ensure_initialized()

    assert _local_record(tmp_path) == before
    assert store.get("TOKEN") == "value"


def test_a_record_that_exists_but_cannot_be_read_is_an_error(fake_keyring, tmp_path):
    """Starting over would replace the namespace the record names on the next
    write, leaving its values where nothing records them."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    (tmp_path / ".guildbotics" / "local" / "secrets.json").mkdir(parents=True)

    with pytest.raises(SecretStoreError):
        store.set("TOKEN", "value")

    assert fake_keyring.passwords == {}


def test_the_index_lists_the_keys_and_every_operation_uses_the_namespace(
    fake_keyring, tmp_path
):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set_many({"SECOND_TOKEN": "two", "FIRST_TOKEN": "one"})
    store.set("GONE_TOKEN", "three")
    store.delete("GONE_TOKEN")
    namespace = f"GuildBotics/{_local_record(tmp_path)['store_id']}"

    assert load_yaml_dict(store.location) == {"keys": ["FIRST_TOKEN", "SECOND_TOKEN"]}
    assert store.keys() == ["FIRST_TOKEN", "SECOND_TOKEN"]
    assert fake_keyring.passwords == {
        (namespace, "FIRST_TOKEN"): "one",
        (namespace, "SECOND_TOKEN"): "two",
    }
    assert store.values() == {"FIRST_TOKEN": "one", "SECOND_TOKEN": "two"}


def test_a_listed_key_without_a_value_reads_as_absent(fake_keyring, tmp_path):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    store.location.write_text(
        dump_yaml({"keys": ["OTHER_TOKEN", "TOKEN"]}), encoding="utf-8"
    )

    assert store.get("OTHER_TOKEN") is None
    assert store.values() == {"TOKEN": "value"}


def test_no_secret_value_reaches_a_workspace_file(fake_keyring, tmp_path):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value-that-must-stay-in-the-keychain")

    for path in (tmp_path / ".guildbotics").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert "must-stay" not in text, path


@pytest.mark.parametrize(
    "local_text",
    [
        json.dumps({"keys": {}}),
        json.dumps({"store_id": ""}),
        json.dumps({"store_id": 123}),
        "{not json",
        json.dumps(["store_id"]),
    ],
    ids=["no-namespace", "empty-namespace", "not-a-name", "unparsable", "not-a-record"],
)
def test_a_record_that_names_no_namespace_stops_every_operation(
    fake_keyring, tmp_path, local_text
):
    """A fresh namespace would leave the values under the old one where
    nothing records them, so only a missing record starts one."""
    config_dir = tmp_path / ".guildbotics" / "config"
    local = tmp_path / ".guildbotics" / "local" / "secrets.json"
    local.parent.mkdir(parents=True)
    local.write_text(local_text, encoding="utf-8")
    store = KeyringSecretStore(config_dir)

    for operation in (
        lambda: store.get("TOKEN"),
        lambda: store.set("TOKEN", "value"),
        lambda: store.delete("TOKEN"),
        store.ensure_initialized,
    ):
        with pytest.raises(SecretStoreError):
            operation()

    assert local.read_text(encoding="utf-8") == local_text
    assert not store.location.exists()
    assert fake_keyring.passwords == {}


def test_a_damaged_namespace_record_leaves_the_index_as_it_was(fake_keyring, tmp_path):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    index = store.location.read_bytes()
    (tmp_path / ".guildbotics" / "local" / "secrets.json").write_text(
        "{not json", encoding="utf-8"
    )

    with pytest.raises(SecretStoreError):
        store.delete("TOKEN")

    assert store.location.read_bytes() == index
    assert len(fake_keyring.passwords) == 1


def test_deleting_an_unknown_key_records_no_namespace(fake_keyring, tmp_path):
    """A namespace no value was written under is not one to keep."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")

    store.delete("UNKNOWN")

    assert not (tmp_path / ".guildbotics" / "local" / "secrets.json").exists()
    assert not store.location.exists()


@pytest.mark.parametrize(
    "index_bytes",
    [
        b"keys:\n  TOKEN:\n    generation: 1\n",
        b"keys: [TOKEN]\nTOKEN: a-value\n",
        b"keys: [TOKEN\n",
        b"- TOKEN\n",
        b"",
        b"keys: [1]\n",
        b"keys: [\xff\xfe]\n",
    ],
    ids=[
        "old-mapping",
        "extra-field",
        "syntax-error",
        "top-level-list",
        "empty",
        "not-names",
        "not-utf-8",
    ],
)
def test_an_index_that_is_not_a_list_of_names_stops_every_operation(
    fake_keyring, tmp_path, index_bytes
):
    """Read as no keys, the next write would replace the index and every
    secret would silently go missing."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.location.parent.mkdir(parents=True)
    store.location.write_bytes(index_bytes)

    for operation in (
        store.keys,
        store.values,
        store.ensure_initialized,
        lambda: store.set("NEW_TOKEN", "value"),
        lambda: store.delete("TOKEN"),
    ):
        with pytest.raises(SecretStoreError):
            operation()

    assert store.location.read_bytes() == index_bytes
    assert not (tmp_path / ".guildbotics" / "local" / "secrets.json").exists()
    assert fake_keyring.passwords == {}


def test_an_index_whose_directory_cannot_be_searched_is_not_missing(
    fake_keyring, tmp_path, posix_permissions
):
    """``exists()`` answers "missing" for a file it may not look at, and the
    read-only callers would then see no names at all."""
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    store.location.parent.chmod(0)
    try:
        with pytest.raises(SecretStoreError):
            store.keys()
    finally:
        store.location.parent.chmod(0o755)
    assert store.keys() == ["TOKEN"]


def test_an_index_that_cannot_be_read_stops_the_write(
    fake_keyring, tmp_path, monkeypatch
):
    store = KeyringSecretStore(tmp_path / ".guildbotics" / "config")
    store.set("TOKEN", "value")
    before = store.location.read_bytes()
    real_open = Path.open

    def unreadable(path, *args, **kwargs):
        if path == store.location and "w" not in (args[0] if args else "r"):
            raise PermissionError("index temporarily unreadable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)

    with pytest.raises(SecretStoreError):
        store.set("NEW_TOKEN", "value")

    monkeypatch.undo()
    assert store.location.read_bytes() == before
    namespace = f"GuildBotics/{_local_record(tmp_path)['store_id']}"
    assert fake_keyring.passwords == {(namespace, "TOKEN"): "value"}
