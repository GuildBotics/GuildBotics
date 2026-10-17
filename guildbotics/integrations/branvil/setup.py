"""Branvil's setup: the items of its services and a member's Branvil account.

Every machine reaches Branvil at the same base URL (on a machine other than
the Hub, Branvil's tunnel forwards it), so the URL the project stores is the
one its records name.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator

from guildbotics.entities.team import Service
from guildbotics.integrations.branvil.api import DEFAULT_BASE_URL
from guildbotics.integrations.provider import MemberAccount, ProviderSetup, SetupItems
from guildbotics.integrations.repository_scope import valid_path

#: Where Branvil is reached: an origin, which its URLs start with.
_BASE_URL = re.compile(r"https?://[^/\s?#@]+")
#: A member's name on Branvil.
_MEMBER = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class CodeHosting(SetupItems):
    """The Branvil the members reach, and the namespace they write in."""

    base_url: str = DEFAULT_BASE_URL
    owner: str = Field(min_length=1)

    @field_validator("base_url")
    @classmethod
    def _origin(cls, value: str) -> str:
        value = value.rstrip("/") or DEFAULT_BASE_URL
        if not _BASE_URL.fullmatch(value):
            raise ValueError("base_url must be an http(s) origin: http://<host>:<port>")
        return value

    @field_validator("owner")
    @classmethod
    def _namespace(cls, value: str) -> str:
        # A namespace is what a path of it starts with.
        if not valid_path(f"{value}/name"):
            raise ValueError("owner must be a namespace: names joined by '/'")
        return value


class Tickets(CodeHosting):
    """As for the code host, and the board tickets are taken from."""

    board: str = ""

    @field_validator("board")
    @classmethod
    def _board(cls, value: str) -> str:
        if value and not valid_path(value):
            raise ValueError("board must be a board path: <namespace>/<name>")
        return value


class AccountView(BaseModel):
    """A member's stored Branvil account, as the member screen shows it."""

    username: str = ""
    has_token: bool = False


class BranvilAccount(MemberAccount):
    """A member's Branvil account: its name, and an agent's token."""

    username: str = ""
    token: str = ""

    @field_validator("username")
    @classmethod
    def _name(cls, value: str) -> str:
        if value and not _MEMBER.fullmatch(value):
            raise ValueError("username must be a Branvil member name")
        return value

    def stored(
        self, person_id: str, human: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        info = {"branvil_username": self.username} if self.username else {}
        token = {} if human or not self.token else {"BRANVIL_TOKEN": self.token}
        return info, token

    @classmethod
    def view(
        cls, account_info: Mapping[str, Any], has_secret: Callable[[str], bool]
    ) -> AccountView:
        return AccountView(
            username=str(account_info.get("branvil_username", "")),
            has_token=has_secret("BRANVIL_TOKEN"),
        )


SETUP = ProviderSetup(
    title="Branvil",
    services={
        Service.CODE_HOSTING_SERVICE: CodeHosting,
        Service.TICKET_SERVICE: Tickets,
    },
    member=BranvilAccount,
)
