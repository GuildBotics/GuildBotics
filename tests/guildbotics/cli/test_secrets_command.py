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


def _add_member(workspace: Path, person_id: str) -> None:
    member_dir = _config_dir(workspace) / "team" / "members" / person_id
    member_dir.mkdir(parents=True)
    (member_dir / "person.yml").write_text(f"person_id: {person_id}\n")


def test_secrets_group_is_registered_on_main_cli(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        main, ["secrets", "--workspace", str(workspace), "status"]
    )

    assert result.exit_code == 0
    assert "backend: env-file" in result.output


def test_status_reports_legacy_backend_and_migrate_hint(
    fake_keyring, tmp_path, monkeypatch
):
    workspace = _workspace(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "status"]
    )

    assert result.exit_code == 0
    assert "backend: env-file (legacy default)" in result.output
    assert "keychain available: yes" in result.output
    assert "guildbotics secrets migrate" in result.output


def test_migrate_moves_managed_secrets_to_keychain(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        env_lines=(
            "OPENAI_API_KEY=sk-secret\n"
            "ALICE_GITHUB_ACCESS_TOKEN=ghp-secret\n"
            "ALICE_GITHUB_INSTALLATION_ID=42\n"
            "LOG_LEVEL=debug\n"
        ),
    )
    _add_member(workspace, "alice")

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "migrate"]
    )

    assert result.exit_code == 0, result.output
    store = KeyringSecretStore(_config_dir(workspace))
    assert store.get("OPENAI_API_KEY") == "sk-secret"
    assert store.get("ALICE_GITHUB_ACCESS_TOKEN") == "ghp-secret"
    env_values = read_env_values(workspace / ".env")
    assert "OPENAI_API_KEY" not in env_values
    assert "ALICE_GITHUB_ACCESS_TOKEN" not in env_values
    assert env_values["ALICE_GITHUB_INSTALLATION_ID"] == "42"
    assert env_values["LOG_LEVEL"] == "debug"
    assert configured_secrets_backend(_config_dir(workspace)) == "keyring"


def test_migrate_copies_private_key_file_content(fake_keyring, tmp_path, monkeypatch):
    pem_file = tmp_path / "alice.pem"
    pem_file.write_text("-----BEGIN RSA PRIVATE KEY-----\npem\n")
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        env_lines=f"ALICE_GITHUB_PRIVATE_KEY_PATH={pem_file}\n",
    )

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "migrate"])

    assert result.exit_code == 0, result.output
    store = KeyringSecretStore(_config_dir(workspace))
    assert store.get("ALICE_GITHUB_PRIVATE_KEY") == pem_file.read_text()
    # The path entry is dropped from .env (the keychain content replaces the
    # file); only deleting the PEM file itself is left to the user.
    assert pem_file.exists()
    env_values = read_env_values(workspace / ".env")
    assert "ALICE_GITHUB_PRIVATE_KEY_PATH" not in env_values
    assert "delete them manually" in result.output


def test_migrate_warns_on_unreadable_private_key_file(
    fake_keyring, tmp_path, monkeypatch
):
    workspace = _workspace(
        tmp_path,
        monkeypatch,
        env_lines=f"ALICE_GITHUB_PRIVATE_KEY_PATH={tmp_path / 'missing.pem'}\n",
    )

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "migrate"])

    assert result.exit_code == 0, result.output
    assert "warning: skipped ALICE_GITHUB_PRIVATE_KEY" in result.output
    assert KeyringSecretStore(_config_dir(workspace)).get(
        "ALICE_GITHUB_PRIVATE_KEY"
    ) is None


def test_migrate_supports_extra_keys(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(
        tmp_path, monkeypatch, env_lines="CUSTOM_SERVICE_TOKEN=custom-secret\n"
    )

    result = CliRunner().invoke(
        secrets,
        ["--workspace", str(workspace), "migrate", "--key", "CUSTOM_SERVICE_TOKEN"],
    )

    assert result.exit_code == 0, result.output
    store = KeyringSecretStore(_config_dir(workspace))
    assert store.get("CUSTOM_SERVICE_TOKEN") == "custom-secret"
    assert "CUSTOM_SERVICE_TOKEN" not in read_env_values(workspace / ".env")


def test_migrate_fails_without_keychain(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    monkeypatch.delenv("GUILDBOTICS_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(secrets_cli, "keyring_available", lambda: False)

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "migrate"]
    )

    assert result.exit_code != 0
    assert "keychain" in result.output.lower()


def test_migrate_fails_when_env_file_backend_is_forced(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("GUILDBOTICS_SECRETS_BACKEND", "env-file")

    result = CliRunner().invoke(
        secrets, ["--workspace", str(workspace), "migrate"]
    )

    assert result.exit_code != 0
    assert "GUILDBOTICS_SECRETS_BACKEND" in result.output


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


def test_export_to_stdout(fake_keyring, tmp_path, monkeypatch):
    workspace = _workspace(tmp_path, monkeypatch)
    KeyringSecretStore(_config_dir(workspace)).set("OPENAI_API_KEY", "sk-secret")

    result = CliRunner().invoke(secrets, ["--workspace", str(workspace), "export"])

    assert result.exit_code == 0
    assert "OPENAI_API_KEY=sk-secret" in result.output
