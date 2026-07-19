from __future__ import annotations

from pathlib import Path

import importlib

from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.cli.secrets import secrets

secrets_cli = importlib.import_module("guildbotics.cli.secrets")
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    configured_secrets_backend,
    read_env_values,
)


def _workspace(tmp_path: Path, monkeypatch, *, env_lines: str = "") -> Path:
    """Create a workspace dir and keep runtime env vars from leaking out."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for key in ("GUILDBOTICS_CONFIG_DIR", "GUILDBOTICS_ENV_FILE"):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    workspace = tmp_path / "workspace"
    (workspace / ".guildbotics" / "config").mkdir(parents=True)
    (workspace / ".env").write_text(env_lines)
    return workspace


def _config_dir(workspace: Path) -> Path:
    return workspace / ".guildbotics" / "config"


def test_secrets_group_is_registered_on_main_cli(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main, ["secrets", "--workspace", str(workspace), "status"]
    )

    assert result.exit_code == 0
    assert "backend: env-file" in result.output


def test_status_reports_default_backend(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "status"]
    )

    assert result.exit_code == 0
    assert "backend: env-file (default)" in result.output
    assert "keychain available: yes" in result.output


def test_set_stores_in_keychain_and_strips_env_file(
    fake_keyring, tmp_path, monkeypatch
):
    workspace = _workspace(
        tmp_path, monkeypatch, env_lines="MY_TOKEN=plaintext\nLOG_LEVEL=debug\n"
    )
    KeyringSecretStore(_config_dir(workspace)).ensure_initialized()

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "set", "MY_TOKEN", "new-secret"]
    )

    assert result.exit_code == 0, result.output
    assert KeyringSecretStore(_config_dir(workspace)).get("MY_TOKEN") == "new-secret"
    env_values = read_env_values(workspace / ".env")
    assert "MY_TOKEN" not in env_values
    assert env_values["LOG_LEVEL"] == "debug"


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


def test_export_to_stdout(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    KeyringSecretStore(_config_dir(workspace)).set("OPENAI_API_KEY", "sk-secret")

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "export"])

    assert result.exit_code == 0
    assert "OPENAI_API_KEY=sk-secret" in result.output
