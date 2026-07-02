"""Local API daemon for GuildBotics desktop clients."""

from typing import Any

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from guildbotics.app_api.api import create_app

        return create_app
    raise AttributeError(name)
