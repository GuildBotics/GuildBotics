"""The two guarantees every payload must satisfy before it is recorded.

Shared records -- activity events, task runs, diagnostics answers -- are kept
in the workspace and shown on the screens, so this protects exactly two
things -- and deliberately nothing more:

1. secret values must not enter a durable record, and
2. the records must not grow without bound.

Both are enforced by walking values rather than by naming fields. A field-name
rule only covers the fields somebody remembered, so a new payload shape, a new
event, or a secret quoted inside an error message would slip past it; masking
the values themselves and bounding every string cannot be outgrown that way.
Bulk log bodies are the one key-based exception: they are dropped because the
local diagnostics already hold them, not because of what they might contain.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    get_identity_config_dir,
    get_workspace_config_dir,
)

#: Every shared string is truncated here. Full console and prompt bodies stay
#: in device-local diagnostics.
MAX_SHARED_TEXT_CHARS = 500
#: Values shorter than this are too collision-prone to mask ("1", "true", ...).
_MIN_MASKED_LENGTH = 8
#: Bulk log bodies, dropped wholesale rather than truncated.
_LOCAL_ONLY_KEYS = frozenset({"stdout", "stderr", "prompt", "response", "messages"})


def redact_for_sharing(value: Any) -> Any:
    """Return ``value`` with secrets masked, strings bounded, and log bodies dropped.

    Args:
        value (Any): Any JSON-shaped payload: dicts, lists, and scalars.

    Returns:
        Any: The same shape, safe to write into shared state.
    """
    return _redact(value, stored_secret_values())


def stored_secret_values() -> tuple[str, ...]:
    """Secret values currently visible to this process, longest first.

    Read from ``os.environ`` for the keys named in the secrets indexes -- the
    identities', which hold every member's credentials, and the selected
    workspace's -- the realistic way a secret ends up inside an error message,
    so recording never has to open the OS keychain.
    """
    try:
        from guildbotics.utils.secret_store import KeyringSecretStore

        keys: set[str] = set()
        for config_dir in secret_config_dirs():
            keys.update(KeyringSecretStore(config_dir).keys())
    except Exception:
        return ()
    values = {
        value
        for key in keys
        if (value := os.environ.get(key, "")) and len(value) >= _MIN_MASKED_LENGTH
    }
    return tuple(sorted(values, key=len, reverse=True))


def secret_config_dirs() -> tuple[Path, ...]:
    """The config directories whose secrets index names a secret this process
    may hold: the identities', and the selected workspace's when one is."""
    try:
        return (get_identity_config_dir(), get_workspace_config_dir())
    except WorkspaceNotConfiguredError:
        return (get_identity_config_dir(),)


def _redact(value: Any, secret_values: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact(item, secret_values)
            for key, item in value.items()
            if str(key) not in _LOCAL_ONLY_KEYS
        }
    if isinstance(value, list):
        return [_redact(item, secret_values) for item in value]
    if isinstance(value, str):
        masked = value
        for secret in secret_values:
            if secret in masked:
                masked = masked.replace(secret, "***")
        return masked[:MAX_SHARED_TEXT_CHARS]
    return value
