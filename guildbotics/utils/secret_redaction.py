"""Mask the current process's secrets without opening an OS keychain."""

import os
from collections.abc import Iterable
from pathlib import Path

from guildbotics.utils.fileio import load_yaml_dict
from guildbotics.utils.secret_store import is_secret_env_key, register_secret_env_keys

MIN_SECRET_VALUE_LENGTH = 4


def register_secret_names(directories: Iterable[Path]) -> None:
    """Register names from shared indexes, reading no stored values."""
    for directory in directories:
        keys = load_yaml_dict(directory / "secrets.yml").get("keys", {})
        if isinstance(keys, dict):
            register_secret_env_keys(key for key in keys if isinstance(key, str))


def redact_secrets(text: str) -> str:
    """Mask values in longest-first order, including non-pattern secret names."""
    values = sorted(
        {
            value
            for key, value in os.environ.items()
            if is_secret_env_key(key) and len(value) >= MIN_SECRET_VALUE_LENGTH
        },
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "***")
    return text
