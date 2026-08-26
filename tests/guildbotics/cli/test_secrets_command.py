from __future__ import annotations

from pathlib import Path

import importlib

import i18n
from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli.secrets import secrets

secrets_cli = importlib.import_module("guildbotics.cli.secrets")
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    read_env_values,
)
from guildbotics.utils.i18n_tool import set_language, t
from guildbotics.utils.windows_credentials import WindowsCredentialManager


class FakeCredentialApi:
    def __init__(self):
        self.values: dict[str, bytes] = {}

    def read(self, target: str) -> bytes | None:
        return self.values.get(target)

    def write(self, target: str, username: str, blob: bytes) -> None:
        del username
        self.values[target] = blob

    def delete(self, target: str) -> None:
        self.values.pop(target, None)


def _workspace(tmp_path: Path, monkeypatch, *, env_lines: str = "") -> Path:
    """Create a workspace dir and keep runtime env vars from leaking out."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("GUILDBOTICS_CONFIG_DIR",):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    workspace = tmp_path / "workspace"
    (workspace / ".guildbotics" / "config").mkdir(parents=True)
    return workspace


def _config_dir(workspace: Path) -> Path:
    return workspace / ".guildbotics" / "config"


def test_secrets_group_is_registered_on_main_cli(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main, ["secrets", "--workspace", str(workspace), "status"]
    )

    assert result.exit_code == 0
    assert "os_secret_store: available" in result.output


def test_status_reports_os_secret_store(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "status"])

    assert result.exit_code == 0
    assert "os_secret_store: available" in result.output
    assert "locked: no" in result.output


def test_set_stores_in_keychain(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    KeyringSecretStore(_config_dir(workspace)).ensure_initialized()

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "set", "MY_TOKEN", "new-secret"]
    )

    assert result.exit_code == 0, result.output
    assert KeyringSecretStore(_config_dir(workspace)).get("MY_TOKEN") == "new-secret"


def test_list_and_delete(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    store = KeyringSecretStore(_config_dir(workspace))
    store.set("B_TOKEN", "b")
    store.set("A_TOKEN", "a")
    runner = CliRunner()

    listed = runner.invoke(secrets, ["--workspace", str(workspace), "list"])
    assert listed.exit_code == 0
    assert listed.output.splitlines() == ["A_TOKEN", "B_TOKEN"]

    deleted = runner.invoke(
        secrets, ["--workspace", str(workspace), "delete", "A_TOKEN"]
    )
    assert deleted.exit_code == 0
    assert store.keys() == ["B_TOKEN"]


def test_export_and_import_roundtrip(fake_keyring, tmp_path, monkeypatch):
    source = _workspace(tmp_path, monkeypatch)
    store = KeyringSecretStore(_config_dir(source))
    store.set("OPENAI_API_KEY", "sk-secret")
    runner = CliRunner()
    export_file = tmp_path / "secrets-export.env"

    exported = runner.invoke(
        secrets,
        ["--workspace", str(source), "export", "--file", str(export_file)],
    )
    assert exported.exit_code == 0, exported.output
    assert read_env_values(export_file) == {"OPENAI_API_KEY": "sk-secret"}

    target = tmp_path / "target"
    (target / ".guildbotics" / "config").mkdir(parents=True)
    KeyringSecretStore(_config_dir(target)).ensure_initialized()
    imported = runner.invoke(
        secrets, ["--workspace", str(target), "import", str(export_file)]
    )
    assert imported.exit_code == 0, imported.output
    assert KeyringSecretStore(_config_dir(target)).get("OPENAI_API_KEY") == "sk-secret"


def test_export_and_import_roundtrip_multiline_pem(fake_keyring, tmp_path, monkeypatch):
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        'MIIEow+abc/123 "quoted" back\\slash\n'
        "-----END RSA PRIVATE KEY-----\n"
    )
    source = _workspace(tmp_path, monkeypatch)
    KeyringSecretStore(_config_dir(source)).set("ALICE_GITHUB_PRIVATE_KEY", pem)
    runner = CliRunner()
    export_file = tmp_path / "secrets-export.env"

    exported = runner.invoke(
        secrets, ["--workspace", str(source), "export", "--file", str(export_file)]
    )
    assert exported.exit_code == 0, exported.output
    assert read_env_values(export_file) == {"ALICE_GITHUB_PRIVATE_KEY": pem}

    target = tmp_path / "target"
    (target / ".guildbotics" / "config").mkdir(parents=True)
    KeyringSecretStore(_config_dir(target)).ensure_initialized()
    imported = runner.invoke(
        secrets, ["--workspace", str(target), "import", str(export_file)]
    )
    assert imported.exit_code == 0, imported.output
    assert KeyringSecretStore(_config_dir(target)).get("ALICE_GITHUB_PRIVATE_KEY") == (
        pem
    )


def test_windows_import_accepts_large_pem(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    KeyringSecretStore(_config_dir(workspace), keychain=manager).ensure_initialized()
    monkeypatch.setattr(
        secrets_cli,
        "resolve_secret_store",
        lambda *_args, **_kwargs: KeyringSecretStore(
            _config_dir(workspace), keychain=manager
        ),
    )
    pem = "x" * 1679
    import_file = tmp_path / "secrets.env"
    import_file.write_text(f'ALICE_GITHUB_PRIVATE_KEY="{pem}"\n')

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "import", str(import_file)]
    )

    assert result.exit_code == 0, result.output
    assert (
        KeyringSecretStore(_config_dir(workspace), keychain=manager).get(
            "ALICE_GITHUB_PRIVATE_KEY"
        )
        == pem
    )


def test_windows_import_prevalidates_all_values(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    api = FakeCredentialApi()
    manager = WindowsCredentialManager(api)
    KeyringSecretStore(_config_dir(workspace), keychain=manager).ensure_initialized()
    monkeypatch.setattr(
        secrets_cli,
        "resolve_secret_store",
        lambda *_args, **_kwargs: KeyringSecretStore(
            _config_dir(workspace), keychain=manager
        ),
    )
    import_file = tmp_path / "secrets.env"
    import_file.write_text(f"SMALL=ok\nTOO_LARGE={'x' * 2561}\n")

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "import", str(import_file)]
    )

    assert result.exit_code == 1
    assert "TOO_LARGE" in result.output
    assert "2561" in result.output
    assert "2560" in result.output
    assert "x" * 100 not in result.output
    assert api.values == {}
    assert KeyringSecretStore(_config_dir(workspace), keychain=manager).keys() == []


def test_secret_size_error_is_translated_in_english_and_japanese():
    previous_locale = i18n.get("locale")
    try:
        set_language("en")
        english = t(
            "cli.secrets.value_too_large",
            secret_key="TOO_LARGE",
            size=2561,
            limit=2560,
        )
        set_language("ja")
        japanese = t(
            "cli.secrets.value_too_large",
            secret_key="TOO_LARGE",
            size=2561,
            limit=2560,
        )
    finally:
        i18n.set("locale", previous_locale)

    assert english == (
        "Secret TOO_LARGE requires 2561 bytes, exceeding the keychain limit of "
        "2560 bytes."
    )
    assert japanese == (
        "シークレット TOO_LARGE は 2561 バイトあり、キーチェーンの上限 2560 "
        "バイトを超えています。"
    )


def test_export_to_stdout(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    KeyringSecretStore(_config_dir(workspace)).set("OPENAI_API_KEY", "sk-secret")

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "export"])

    assert result.exit_code == 0
    assert "OPENAI_API_KEY=sk-secret" in result.output


class TestHubTransfers:
    """``secrets push`` / ``secrets pull``, which a machine with no Desktop uses.

    A headless Linux device joins a workspace, finds it holds none of the
    values, and gets them here. So the commands are checked for the two things
    that matter on such a machine: the value really arrives in the OS secret
    store, and it appears in no output.
    """

    def _connected(self, tmp_path, monkeypatch, hub_secrets, order=None):
        """A workspace whose hub is this same machine."""
        workspace = _workspace(tmp_path, monkeypatch)
        monkeypatch.setattr(
            secrets_cli,
            "hub_remote_url",
            lambda *_: f"/hub/{WORKSPACE_ID}/repository.git",
        )
        monkeypatch.setattr(secrets_cli, "hub_secret_client", lambda *_: hub_secrets)
        monkeypatch.setattr(secrets_cli, "commit_and_push_once", lambda **_: None)
        monkeypatch.setattr(
            secrets_cli,
            "synchronize_once",
            lambda **_: order.append("refresh") if order is not None else None,
        )
        hub_secrets.order = order
        return workspace

    def test_push_takes_the_current_shared_files_before_deciding(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        """Which generation a value is sent from is read out of the shared
        index, so a machine that has been offline refreshes it first."""
        order: list[str] = []
        hub = FakeHubSecrets()
        workspace = self._connected(tmp_path, monkeypatch, hub, order)
        KeyringSecretStore(_config_dir(workspace)).set("MY_TOKEN", "ghp-000111")

        CliRunner().invoke(secrets, ["--workspace", str(workspace), "push"])

        assert order == ["refresh", "send"]

    def test_push_offers_a_hub_that_holds_nothing_every_value(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        """A workspace that has only just been connected -- or one whose hub was
        rebuilt -- has given it nothing, so "everything" cannot mean only the
        values typed since."""
        hub = FakeHubSecrets()
        workspace = self._connected(tmp_path, monkeypatch, hub)
        store = KeyringSecretStore(_config_dir(workspace))
        store.set("MY_TOKEN", "ghp-000111")
        store.confirm_shared({"MY_TOKEN": 1}, sent={"MY_TOKEN": "ghp-000111"})

        result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "push"])

        assert result.exit_code == 0, result.output
        assert "MY_TOKEN: sent (generation 2)" in result.output
        assert hub.values == {"MY_TOKEN": "ghp-000111"}

    def test_push_sends_what_was_entered_here(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        hub = FakeHubSecrets()
        workspace = self._connected(tmp_path, monkeypatch, hub)
        KeyringSecretStore(_config_dir(workspace)).set("MY_TOKEN", "ghp-000111")

        result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "push"])

        assert result.exit_code == 0, result.output
        assert hub.values == {"MY_TOKEN": "ghp-000111"}
        assert "MY_TOKEN: sent (generation 1)" in result.output
        assert "ghp-000111" not in result.output

    def test_pull_stores_every_missing_value_without_retyping_any(
        self, fake_keyring, tmp_path, monkeypatch
    ):
        hub = FakeHubSecrets()
        workspace = self._connected(tmp_path, monkeypatch, hub)
        store = KeyringSecretStore(_config_dir(workspace))
        store.set("MY_TOKEN", "ghp-000111")
        runner = CliRunner()
        runner.invoke(secrets, ["--workspace", str(workspace), "push"])
        # The machine that has to fetch knows the key and holds no value.
        (workspace / ".guildbotics" / "local" / "secrets.json").unlink()
        fake_keyring.passwords.clear()

        result = runner.invoke(secrets, ["--workspace", str(workspace), "pull"])

        assert result.exit_code == 0, result.output
        assert "MY_TOKEN: fetched (generation 1)" in result.output
        assert "ghp-000111" not in result.output
        assert (
            KeyringSecretStore(_config_dir(workspace)).get("MY_TOKEN") == "ghp-000111"
        )

    def test_a_workspace_with_no_hub_says_so(self, fake_keyring, tmp_path, monkeypatch):
        workspace = _workspace(tmp_path, monkeypatch)
        monkeypatch.setattr(secrets_cli, "hub_remote_url", lambda *_: None)

        result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "pull"])

        assert result.exit_code != 0
        assert "not connected to a hub" in result.output


WORKSPACE_ID = "0198ab00-0000-7000-8000-000000000001"


class FakeHubSecrets:
    """A hub holding values in memory, with the base check the real one makes."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.held: dict[str, int] = {}
        self.order: list[str] | None = None

    def index(self):
        from guildbotics.secrets.hub_client import HubSecretIndex

        return HubSecretIndex(generations=dict(self.held))

    def send(self, entries):
        from guildbotics.secrets.hub_client import HubSendResult

        if self.order is not None:
            self.order.append("send")
        results = []
        for offer in entries:
            if self.held.get(offer.key, offer.candidate - 1) != offer.candidate - 1:
                results.append(HubSendResult(key=offer.key, status="conflict"))
                continue
            self.values[offer.key] = offer.value
            self.held[offer.key] = offer.candidate
            results.append(
                HubSendResult(
                    key=offer.key, status="stored", generation=offer.candidate
                )
            )
        return results

    def fetch(self, keys):
        from guildbotics.secrets.hub_client import HubFetchResult

        return [
            HubFetchResult(
                key=key,
                status="sent" if key in self.values else "missing",
                generation=self.held.get(key),
                value=self.values.get(key),
            )
            for key in keys
        ]
