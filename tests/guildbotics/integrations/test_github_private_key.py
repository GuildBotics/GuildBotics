"""Resolution of the GitHub App private key (keychain content vs. PEM file)."""

import pytest

from guildbotics.entities.team import Person
from guildbotics.integrations.github.github_utils import get_person_private_key_pem
from guildbotics.utils.secret_store import KeyringSecretStore

PEM_FROM_STORE = "-----BEGIN RSA PRIVATE KEY-----\nstore\n-----END RSA PRIVATE KEY-----\n"
PEM_FROM_FILE = "-----BEGIN RSA PRIVATE KEY-----\nfile\n-----END RSA PRIVATE KEY-----\n"


@pytest.fixture
def person() -> Person:
    return Person(person_id="aiko", name="Aiko")


def _pin_workspace(tmp_path, monkeypatch):
    config_dir = tmp_path / ".guildbotics" / "config"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(tmp_path)
    return config_dir


def test_prefers_keychain_content_over_path(fake_keyring, tmp_path, monkeypatch, person):
    config_dir = _pin_workspace(tmp_path, monkeypatch)
    KeyringSecretStore(config_dir).set("AIKO_GITHUB_PRIVATE_KEY", PEM_FROM_STORE)
    pem_file = tmp_path / "aiko.pem"
    pem_file.write_text(PEM_FROM_FILE)
    monkeypatch.setenv("AIKO_GITHUB_PRIVATE_KEY_PATH", str(pem_file))

    assert get_person_private_key_pem(person) == PEM_FROM_STORE.encode()


def test_falls_back_to_key_file(fake_keyring, tmp_path, monkeypatch, person):
    _pin_workspace(tmp_path, monkeypatch)
    pem_file = tmp_path / "aiko.pem"
    pem_file.write_text(PEM_FROM_FILE)
    monkeypatch.setenv("AIKO_GITHUB_PRIVATE_KEY_PATH", str(pem_file))

    assert get_person_private_key_pem(person) == PEM_FROM_FILE.encode()


def test_key_content_is_not_published_to_environment(
    fake_keyring, tmp_path, monkeypatch, person
):
    import os

    from guildbotics.utils.env_loader import load_guildbotics_env

    config_dir = _pin_workspace(tmp_path, monkeypatch)
    store = KeyringSecretStore(config_dir)
    store.set("AIKO_GITHUB_PRIVATE_KEY", PEM_FROM_STORE)
    store.set("AIKO_GITHUB_ACCESS_TOKEN", "ghp-secret")
    monkeypatch.delenv("AIKO_GITHUB_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("AIKO_GITHUB_PRIVATE_KEY", raising=False)

    load_guildbotics_env(tmp_path, override=True, prefer_env_file=False)

    assert "AIKO_GITHUB_PRIVATE_KEY" not in os.environ
    assert os.environ["AIKO_GITHUB_ACCESS_TOKEN"] == "ghp-secret"
    monkeypatch.delenv("AIKO_GITHUB_ACCESS_TOKEN")
