from __future__ import annotations

import os
from pathlib import Path

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_workspace_config_dir,
    get_workspace_local_path,
)
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    SecretStore,
    is_environment_secret,
    read_env_values,
    resolve_secret_store,
    write_env_values,
)

HOME_ENV_PROTECTED_KEYS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)
ALLOWED_DEBUG_KEYS = frozenset({"LOG_LEVEL", "AGNO_DEBUG"})
DEBUG_ENV_FILENAME = "debug.env"


def workspace_secret_store() -> SecretStore:
    """Resolve the secret store for the selected workspace.

    For consumers that read a secret at the point of use instead of through
    ``os.environ`` (e.g. the GitHub App private key).
    """
    return resolve_secret_store(get_workspace_config_dir())


def read_workspace_secrets(
    *, skip: frozenset[str] | set[str] = frozenset()
) -> dict[str, str]:
    """Read OS-keychain secrets destined for ``os.environ``.

    Keys in ``skip`` (already supplied as real environment variables) are not
    fetched. A missing or locked keychain yields the values read so far instead
    of failing: real environment variables always keep working without one.
    """
    try:
        store = KeyringSecretStore(get_workspace_config_dir())
    except WorkspaceNotConfiguredError:
        return {}
    values: dict[str, str] = {}
    try:
        stored_keys = store.keys()
        for key in stored_keys:
            if not is_environment_secret(key) or key in skip:
                continue
            value = store.get(key)
            if value is not None:
                values[key] = value
    except SecretStoreError as exc:
        get_logger().warning(
            "OS secret store is unavailable; relying on process environment "
            "variables for the remaining secrets: %s",
            exc,
        )
    return values


def debug_env_path(workspace_root: Path | None = None) -> Path:
    """Return the path of the device-local debug settings file."""
    return get_workspace_local_path(DEBUG_ENV_FILENAME, workspace_root=workspace_root)


def read_debug_env(workspace_root: Path | None = None) -> dict[str, str]:
    """Read allowlisted non-secret debug settings from ``local/debug.env``."""
    try:
        path = debug_env_path(workspace_root)
    except WorkspaceNotConfiguredError:
        return {}
    return {
        key: value
        for key, value in read_env_values(path).items()
        if key in ALLOWED_DEBUG_KEYS
    }


def write_debug_env(values: dict[str, str], workspace_root: Path | None = None) -> Path:
    """Replace allowlisted debug settings in ``local/debug.env``."""
    path = debug_env_path(workspace_root)
    current = read_debug_env(workspace_root)
    for key, value in values.items():
        if key not in ALLOWED_DEBUG_KEYS:
            continue
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    write_env_values(path, current)
    return path


def load_guildbotics_env(*, override: bool = False) -> None:
    """Publish OS-keychain secrets and local debug settings into ``os.environ``.

    Pre-existing environment variables win unless ``override`` is set, and are
    then not even read from the keychain, so servers configured purely through
    environment variables run without an OS secret store.
    """
    skip = frozenset() if override else frozenset(os.environ)
    values: dict[str, str] = {}
    values.update(read_debug_env())
    values.update(read_workspace_secrets(skip=skip))
    for key, value in values.items():
        if key in HOME_ENV_PROTECTED_KEYS:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def apply_debug_env_to_process(values: dict[str, str]) -> None:
    """Publish allowlisted debug keys into the current process."""
    for key, value in values.items():
        if key not in ALLOWED_DEBUG_KEYS:
            continue
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
