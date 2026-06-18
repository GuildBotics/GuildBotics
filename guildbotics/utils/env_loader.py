from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

GUILDBOTICS_ENV_FILE = "GUILDBOTICS_ENV_FILE"
HOME_ENV_PROTECTED_KEYS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)


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
    """Load GuildBotics .env and publish its absolute path for child processes."""
    env_file = resolve_guildbotics_env_file(cwd, prefer_env_file=prefer_env_file)
    if env_file is None:
        return None
    values = dotenv_values(env_file)
    for key, value in values.items():
        if value is None or key in HOME_ENV_PROTECTED_KEYS:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
    os.environ[GUILDBOTICS_ENV_FILE] = str(env_file)
    return env_file
