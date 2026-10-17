"""Storing members in tests the way GuildBotics stores them.

A member is two files: its identity, which every workspace shares, and the
workspace's file of its participation. Tests describe a member whole -- as
GuildBotics loads it -- and this splits it.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from guildbotics.entities.team import IDENTITY_FIELDS, PARTICIPATION_FIELDS
from guildbotics.utils.person_id import IDENTITY_FILE, MEMBER_FILE, identity_members_dir


def write_member(config_dir: Path, person: Mapping[str, Any] | str) -> Path:
    """Store ``person`` -- a whole member, or its YAML -- for the workspace whose
    config directory is ``config_dir``.

    Returns:
        Path: The workspace's directory of the member.
    """
    data = dict(yaml.safe_load(person) if isinstance(person, str) else person)
    person_id = str(data["person_id"])
    profile = dict(data.pop("profile", None) or {})
    roles = profile.pop("roles", None)
    identity = {key: value for key, value in data.items() if key in IDENTITY_FIELDS}
    if profile:
        identity["profile"] = profile
    participation = {
        key: value for key, value in data.items() if key in PARTICIPATION_FIELDS
    }
    if roles is not None:
        participation["profile"] = {"roles": roles}
    unknown = set(data) - IDENTITY_FIELDS - PARTICIPATION_FIELDS
    assert not unknown, f"not a member field: {sorted(unknown)}"
    identity_dir = identity_members_dir() / person_id
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / IDENTITY_FILE).write_text(
        yaml.safe_dump(identity, allow_unicode=True), encoding="utf-8"
    )
    member_dir = config_dir / "team/members" / person_id
    member_dir.mkdir(parents=True, exist_ok=True)
    (member_dir / MEMBER_FILE).write_text(
        yaml.safe_dump(participation, allow_unicode=True), encoding="utf-8"
    )
    return member_dir
