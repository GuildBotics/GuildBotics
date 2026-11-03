"""Migration uses synthetic values and never the real OS keychain."""

import base64
import json
from pathlib import Path

import pytest

from guildbotics.environment import secret_migration as migration
from guildbotics.utils.keychain import SecretStoreError, system_keychain
from guildbotics.utils.secret_store import KeyringSecretStore, read_secret_index

KEY = bytes([19] * 32)
GOLDEN = Path(__file__).resolve().parents[3] / "records/golden/secret_migration.json"


def prepare(root, backend):
    identity, workspace = root / "identity", root / "workspace"
    backend.set_password(*migration.DEVICE_ENTRY, base64.b64encode(KEY).decode())
    for path, namespace, values in [
        (identity, "synthetic-identity", {"MEMBER_TOKEN": "synthetic-member-名"}),
        (
            workspace,
            "synthetic-workspace",
            {"API_KEY": "synthetic-api\nline", "on": ""},
        ),
    ]:
        store = KeyringSecretStore(path / ".guildbotics/config")
        store._write_local({"store_id": namespace})
        store.set_many(values)
    backend.set_password(
        migration.GIT_SERVICE, "https://synthetic.invalid", "synthetic-git"
    )
    return identity, workspace


def run(identity, workspace, backend):
    return migration.migrate(
        identity,
        [workspace],
        keychain=system_keychain(),
        inventory=lambda: list(backend.passwords),
        device_key=KEY,
    )


def result_records(identity, workspace):
    return [
        {
            "scope": scope,
            "index": (root / ".guildbotics/config/secrets.yml").read_text(),
            "local": (root / ".guildbotics/local/secrets.json").read_text(),
            "sealed": json.loads(
                (root / ".guildbotics/local/secrets.enc.json").read_bytes()
            ),
            "values": migration._open(root, migration._namespace(root), KEY),
        }
        for scope, root in [("identity", identity), ("workspace", workspace)]
    ]


def test_moves_verifies_audits_and_retries(tmp_path, fake_keyring):
    identity, workspace = prepare(tmp_path, fake_keyring)
    assert run(identity, workspace, fake_keyring) == {
        "stores": 2,
        "removed": 4,
        "remaining": 1,
    }
    assert list(fake_keyring.passwords) == [migration.DEVICE_ENTRY]
    for record in result_records(identity, workspace):
        assert set(json.loads(record["index"])["keys"]) == set(record["values"])
        assert "synthetic-" not in json.dumps(record["sealed"])
    assert run(identity, workspace, fake_keyring)["removed"] == 0


@pytest.mark.parametrize(
    "failure", ["unknown", "missing", "key", "namespace", "corrupt", "conflict"]
)
def test_preflight_stops_without_deleting(tmp_path, fake_keyring, failure):
    identity, workspace = prepare(tmp_path, fake_keyring)
    if failure == "unknown":
        fake_keyring.set_password("GuildBotics/forgotten", "OTHER", "synthetic-other")
    elif failure == "missing":
        del fake_keyring.passwords[("GuildBotics/synthetic-workspace", "API_KEY")]
    elif failure == "key":
        fake_keyring.passwords[migration.DEVICE_ENTRY] = "synthetic-bad-key"
    elif failure == "namespace":
        (workspace / ".guildbotics/local/secrets.json").write_text("{}")
    elif failure == "corrupt":
        (workspace / ".guildbotics/local/secrets.enc.json").write_text("{}")
    else:
        run(identity, workspace, fake_keyring)
        fake_keyring.set_password(
            "GuildBotics/synthetic-workspace", "API_KEY", "synthetic-different"
        )
    before = dict(fake_keyring.passwords)
    with pytest.raises(SecretStoreError) as error:
        run(identity, workspace, fake_keyring)
    assert all(value not in str(error.value) for value in before.values() if value)
    assert fake_keyring.passwords == before


@pytest.mark.parametrize("stop_at", range(1, 5))
def test_interrupted_cleanup_retains_all_values_and_can_resume(
    tmp_path, fake_keyring, monkeypatch, stop_at
):
    identity, workspace = prepare(tmp_path, fake_keyring)
    original = fake_keyring.delete_password
    count = 0

    def interrupt(service, account):
        nonlocal count
        original(service, account)
        count += 1
        if count == stop_at:
            raise RuntimeError("synthetic-private-error")

    monkeypatch.setattr(fake_keyring, "delete_password", interrupt)
    with pytest.raises(SecretStoreError):
        run(identity, workspace, fake_keyring)
    assert sum(len(r["values"]) for r in result_records(identity, workspace)) == 4
    monkeypatch.setattr(fake_keyring, "delete_password", original)
    assert run(identity, workspace, fake_keyring)["remaining"] == 1


@pytest.mark.parametrize("stop_at", range(1, 5))
def test_interrupted_writes_never_delete_source(
    tmp_path, fake_keyring, monkeypatch, stop_at
):
    identity, workspace = prepare(tmp_path, fake_keyring)
    before = dict(fake_keyring.passwords)
    count = 0
    atomic, shared = migration.atomic_write_bytes, migration.write_shared_json

    def interrupt(writer, *args, **kwargs):
        nonlocal count
        writer(*args, **kwargs)
        count += 1
        if count == stop_at:
            raise OSError("synthetic-private-error")

    monkeypatch.setattr(
        migration, "atomic_write_bytes", lambda *a, **kw: interrupt(atomic, *a, **kw)
    )
    monkeypatch.setattr(
        migration, "write_shared_json", lambda *a, **kw: interrupt(shared, *a, **kw)
    )
    with pytest.raises(SecretStoreError):
        run(identity, workspace, fake_keyring)
    assert fake_keyring.passwords == before
    monkeypatch.setattr(migration, "atomic_write_bytes", atomic)
    monkeypatch.setattr(migration, "write_shared_json", shared)
    assert run(identity, workspace, fake_keyring)["remaining"] == 1


def test_python_migration_matches_golden(tmp_path, fake_keyring, monkeypatch):
    identity, workspace = prepare(tmp_path, fake_keyring)
    monkeypatch.setattr(migration.os, "urandom", lambda size: bytes([7] * size))
    run(identity, workspace, fake_keyring)
    assert result_records(identity, workspace) == json.loads(GOLDEN.read_bytes())


def test_unsupported_inventory_stops(fake_platform):
    fake_platform(migration, "linux")
    with pytest.raises(SecretStoreError, match="macOS"):
        migration.macos_inventory()


@pytest.mark.parametrize("failure", ["readback", "delete", "residual", "locked"])
def test_verification_and_backend_failures_are_not_success(
    tmp_path, fake_keyring, monkeypatch, failure
):
    identity, workspace = prepare(tmp_path, fake_keyring)
    before = dict(fake_keyring.passwords)
    if failure == "readback":
        original = migration._open

        def damaged_readback(root, store_id, key):
            result = original(root, store_id, key)
            return {"WRONG": "synthetic-private"} if result else result

        monkeypatch.setattr(migration, "_open", damaged_readback)
    elif failure == "delete":
        monkeypatch.setattr(fake_keyring, "delete_password", lambda *a: None)
    elif failure == "locked":

        def locked(*args):
            raise RuntimeError("synthetic-private-backend-detail")

        monkeypatch.setattr(fake_keyring, "get_password", locked)
    else:
        original = fake_keyring.delete_password

        def added_during_cleanup(*args):
            original(*args)
            fake_keyring.set_password("GuildBotics/unexpected", "NEW", "synthetic-new")

        monkeypatch.setattr(fake_keyring, "delete_password", added_during_cleanup)
    with pytest.raises(SecretStoreError) as error:
        run(identity, workspace, fake_keyring)
    assert all(value not in str(error.value) for value in before.values() if value)
    if failure != "residual":
        assert fake_keyring.passwords == before


def test_empty_stores_initialize_and_ciphertext_is_owner_only(tmp_path, fake_keyring):
    identity, workspace = tmp_path / "identity", tmp_path / "workspace"
    fake_keyring.set_password(*migration.DEVICE_ENTRY, base64.b64encode(KEY).decode())
    assert run(identity, workspace, fake_keyring)["remaining"] == 1
    for root in [identity, workspace]:
        assert read_secret_index(root / ".guildbotics/config") == []
        assert migration._open(root, migration._namespace(root), KEY) == {}
        if migration.os.name == "posix":
            assert (
                root / ".guildbotics/local/secrets.enc.json"
            ).stat().st_mode & 0o777 == 0o600


if __name__ == "__main__":
    import tempfile
    from tests.conftest import fake_keyring

    backend_fixture = fake_keyring.__wrapped__()
    backend = next(backend_fixture)
    try:
        with (
            tempfile.TemporaryDirectory() as directory,
            pytest.MonkeyPatch.context() as patch,
        ):
            root = Path(directory)
            patch.setenv("GUILDBOTICS_HOME", str(root / "machine"))
            identity, workspace = prepare(root, backend)
            patch.setattr(migration.os, "urandom", lambda size: bytes([7] * size))
            run(identity, workspace, backend)
            GOLDEN.write_text(
                json.dumps(
                    result_records(identity, workspace), ensure_ascii=False, indent=2
                )
                + "\n"
            )
    finally:
        next(backend_fixture, None)


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("unknown", "unmapped"),
        ("omitted", "unmapped"),
        ("missing", "source_missing"),
        ("conflict", "conflict"),
        ("locked", "backend"),
    ],
)
@pytest.mark.parametrize("language", ["en", "ja"])
def test_cli_explains_safe_reason_and_stage(
    tmp_path, fake_keyring, monkeypatch, capsys, failure, reason, language
):
    from guildbotics.utils.i18n_tool import set_language, t

    set_language(language)
    identity, workspace = prepare(tmp_path, fake_keyring)
    if failure == "unknown":
        fake_keyring.set_password(
            "GuildBotics/orphan", "OPENAI_API_KEY", "synthetic-private-orphan"
        )
    elif failure == "missing":
        del fake_keyring.passwords[("GuildBotics/synthetic-workspace", "API_KEY")]
    elif failure == "conflict":
        run(identity, workspace, fake_keyring)
        fake_keyring.set_password(
            "GuildBotics/synthetic-workspace", "API_KEY", "synthetic-private-conflict"
        )
    elif failure == "locked":

        def locked(*args):
            raise RuntimeError("synthetic-private-backend-detail")

        monkeypatch.setattr(fake_keyring, "get_password", locked)
    monkeypatch.setattr(migration, "_key", lambda **kw: KEY)
    monkeypatch.setattr(
        migration, "macos_inventory", lambda: list(fake_keyring.passwords)
    )
    args = ["migration", "--identity", str(identity), "--writers-stopped"]
    if failure != "omitted":
        args += ["--workspace", str(workspace)]
    monkeypatch.setattr(migration.sys, "argv", args)
    before = dict(fake_keyring.passwords)
    with pytest.raises(SystemExit) as error:
        migration.main()
    assert error.value.code == 1
    output = capsys.readouterr()
    assert not output.out
    assert "preflight" in output.err
    assert t(f"cli.secret_migration.reasons.{reason}") in output.err
    assert all(value not in output.err for value in before.values() if value)
    assert "synthetic-private-backend-detail" not in output.err
    if failure == "unknown":
        assert '"service": "GuildBotics/orphan"' in output.err
        assert '"account": "OPENAI_API_KEY"' in output.err
    if failure == "omitted":
        assert '"service": "GuildBotics/synthetic-workspace"' in output.err
    assert fake_keyring.passwords == before


def test_every_emitted_reason_follows_the_current_language():
    import ast
    import yaml
    from guildbotics.utils.i18n_tool import set_language

    source = Path(migration.__file__)
    emitted = {
        call.args[0].value
        for call in ast.walk(ast.parse(source.read_text()))
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "MigrationError"
        and isinstance(call.args[0], ast.Constant)
    }
    errors = {code: migration.MigrationError(code) for code in emitted}
    for language in ["en", "ja", "en"]:
        set_language(language)
        catalog = yaml.safe_load(
            (
                source.parents[1]
                / f"templates/locales/cli/secret_migration.{language}.yml"
            ).read_text()
        )[language]["reasons"]
        assert set(catalog) == emitted
        for code, error in errors.items():
            assert catalog[code] in str(error)
            assert "preflight" in str(error)
