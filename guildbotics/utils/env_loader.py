from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from guildbotics.utils.secret_store import (
    KEYRING_BACKEND,
    KeyringSecretStore,
    SecretStore,
    configured_secrets_backend,
    is_environment_secret,
    resolve_secret_store,
)

GUILDBOTICS_ENV_FILE = "GUILDBOTICS_ENV_FILE"
HOME_ENV_PROTECTED_KEYS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)


def workspace_config_dir(cwd: Path) -> Path:
    """Resolve the workspace config dir the same way ``fileio`` does."""
    configured = os.getenv("GUILDBOTICS_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return cwd / ".guildbotics" / "config"


def workspace_secret_store(cwd: Path | None = None) -> SecretStore:
    """Resolve the secret store for the workspace around ``cwd``.

    For consumers that read a secret at the point of use instead of through
    ``os.environ`` (e.g. the GitHub App private key).
    """
    if cwd is None:
        cwd = Path.cwd()
    env_file = resolve_guildbotics_env_file(cwd) or cwd / ".env"
    return resolve_secret_store(workspace_config_dir(cwd), env_file)


def read_workspace_secrets(cwd: Path) -> dict[str, str]:
    """Read the workspace's OS-keychain secrets destined for ``os.environ``.

    Empty unless the workspace is configured for the keyring backend. Secrets
    excluded from environment publication (``is_environment_secret``) are
    left out; they are resolved via ``workspace_secret_store`` where needed.
    """
    config_dir = workspace_config_dir(cwd)
    if configured_secrets_backend(config_dir) != KEYRING_BACKEND:
        return {}
    return {
        key: value
        for key, value in KeyringSecretStore(config_dir).values().items()
        if is_environment_secret(key)
    }


def resolve_guildbotics_env_file(
    cwd: Path | None = None, *, prefer_env_file: bool = True
) -> Path | None:
    """Resolve the .env file GuildBotics should load for this process."""
    if cwd is None:
        cwd = Path.cwd()

    if prefer_env_file:
        configured = os.getenv(GUILDBOTICS_ENV_FILE, "").strip()
        if configured:
            path = Path(configured).expanduser()
            if path.is_absolute() and path.is_file():
                return path.resolve()

    dotenv_path = cwd / ".env"
    if dotenv_path.is_file():
        return dotenv_path.resolve()
    return None


def load_guildbotics_env(
    cwd: Path | None = None, *, override: bool = False, prefer_env_file: bool = True
) -> Path | None:
    """Load GuildBotics secrets and .env, publishing the .env path for children.

    OS-keychain secrets win over ``.env`` values; pre-existing environment
    variables win over both unless ``override`` is set.
    """
    if cwd is None:
        cwd = Path.cwd()
    env_file = resolve_guildbotics_env_file(cwd, prefer_env_file=prefer_env_file)
    values: dict[str, str] = {}
    if env_file is not None:
        values.update(
            {
                key: value
                for key, value in dotenv_values(env_file).items()
                if value is not None
            }
        )
    values.update(read_workspace_secrets(cwd))
    for key, value in values.items():
        if key in HOME_ENV_PROTECTED_KEYS:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    if env_file is None:
        return None
    os.environ[GUILDBOTICS_ENV_FILE] = str(env_file)
    return env_file
