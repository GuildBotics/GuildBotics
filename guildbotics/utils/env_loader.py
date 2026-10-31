from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_identity_config_dir,
    get_workspace_config_dir,
    get_workspace_local_path,
)
from guildbotics.utils.keychain import SecretStoreError
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.person_id import ambiguous_person_secret
from guildbotics.utils.secret_store import (
    KeyringSecretStore,
    SecretStore,
    is_environment_secret,
    read_env_values,
    register_secret_env_keys,
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
    """Read OS-keychain secrets destined for ``os.environ``: the identities'
    store, which every workspace shares, then the selected workspace's.

    A key both stores hold is taken from the identities' and the other is
    reported: the members' keys belong there alone, and which of the two a
    process was running with must not depend on load order.

    Keys in ``skip`` (already supplied as real environment variables) are not
    fetched. A missing or locked keychain yields the values read so far instead
    of failing: real environment variables always keep working without one.
    """
    stores = [KeyringSecretStore(get_identity_config_dir())]
    with suppress(WorkspaceNotConfiguredError):
        stores.append(KeyringSecretStore(get_workspace_config_dir()))
    values: dict[str, str] = {}
    ambiguous = ambiguous_person_secret()
    for store in stores:
        try:
            stored_keys = store.keys()
            # Register provenance before the skip decision and before any
            # fetch: a stored key stays classified as secret even when its
            # value is supplied as a real environment variable or the keychain
            # fails here.
            register_secret_env_keys(
                key for key in stored_keys if is_environment_secret(key)
            )
            for key in stored_keys:
                if not is_environment_secret(key) or key in skip or ambiguous(key):
                    continue
                if key in values:
                    get_logger().warning(
                        "%s is stored for the workspace as well as for the "
                        "identities; the identities' value is used.",
                        key,
                    )
                    continue
                value = store.get(key)
                if value is not None:
                    values[key] = value
        except SecretStoreError as exc:
            get_logger().warning(
                "The secrets of %s could not be read; relying on process "
                "environment variables for them: %s",
                store.location,
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
