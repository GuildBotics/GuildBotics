"""Hosting a hub on this machine.

A hub is a directory of bare Git repositories, one per workspace, and nothing
else. It stores no workspace paths and no device list: a device that moves its
workspace root reconnects by workspace identifier alone.

The repositories accept fast-forwards only. That single setting is what makes
concurrent updates settle without asking anyone anything -- the change that
reaches the hub first is the one that stays, and the device whose change lost
the race keeps it locally under a rejected ref instead of overwriting the
winner.
"""

from __future__ import annotations

import getpass
import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

from git import Repo
from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.fileio import atomic_write_text, get_machine_root
from guildbotics.utils.workspace_sync_port import dump_shared_json

#: The branch every workspace repository shares. Users get no branch controls.
HUB_BRANCH = "main"
#: ``hub.json`` is machine-local, so it has a version of its own rather than
#: the shared-record generation that travels between devices.
HUB_SCHEMA_VERSION = 1


class HubSettings(BaseModel):
    """What this machine records about the hub it hosts (``hub.json``).

    Attributes:
        schema_version (int): The layout this file was written with.
        hub_id (str): Identifies this hub, so a device can tell a rebuilt hub
            from the one it was connected to before.
        created_at (str): When the hub was created.
        ssh_endpoint (str): The ``user@host`` this machine suggests to devices.
            It is a starting point for the user, not something the hub can
            verify: only the user knows which name their machines resolve.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=HUB_SCHEMA_VERSION)
    hub_id: str
    created_at: str
    ssh_endpoint: str


def hub_root() -> Path:
    """Return ``~/.guildbotics/hub``, whether or not this machine is a hub."""
    return get_machine_root() / "hub"


def hub_settings_path() -> Path:
    """Return ``~/.guildbotics/hub/hub.json``."""
    return hub_root() / "hub.json"


def workspaces_root() -> Path:
    """Return ``~/.guildbotics/hub/workspaces``."""
    return hub_root() / "workspaces"


def workspace_repository_path(workspace_id: str) -> Path:
    """Return the bare repository directory for one workspace.

    Args:
        workspace_id (str): The workspace identifier, which must be a UUID.

    Raises:
        ValueError: When ``workspace_id`` is not a UUID. Identifiers reach here
            from a remote command line, so anything that could name a directory
            of its own is refused before it becomes a path.
    """
    uuid.UUID(workspace_id)
    return workspaces_root() / workspace_id / "repository.git"


def read_hub() -> HubSettings | None:
    """Return this machine's hub settings, or None when it hosts no hub."""
    path = hub_settings_path()
    if not path.is_file():
        return None
    return HubSettings.model_validate_json(path.read_text(encoding="utf-8"))


def create_hub() -> HubSettings:
    """Make this machine a hub, keeping the settings of one it already hosts.

    Recreating the settings would mint a second hub identifier for what is
    still the same hub, so an existing one is returned untouched.
    """
    existing = read_hub()
    if existing is not None:
        return existing
    settings = HubSettings(
        hub_id=str(uuid.uuid4()),
        created_at=_now(),
        ssh_endpoint=default_ssh_endpoint(),
    )
    workspaces_root().mkdir(parents=True, exist_ok=True)
    atomic_write_text(hub_settings_path(), dump_shared_json(settings.model_dump()))
    return settings


def create_workspace_repository(workspace_id: str) -> Path:
    """Create the bare repository one workspace synchronizes through.

    Idempotent, because a device that lost the answer to its first attempt
    retries, and because two devices may register the same workspace at once.

    Args:
        workspace_id (str): The workspace identifier.

    Returns:
        Path: The repository directory devices push to.

    Raises:
        HubNotHostedError: When this machine is not a hub.
    """
    if read_hub() is None:
        raise HubNotHostedError(
            "This machine is not a hub. Make it one before registering a workspace."
        )
    path = workspace_repository_path(workspace_id)
    if not (path / "HEAD").is_file():
        path.mkdir(parents=True, exist_ok=True)
        Repo.init(path, bare=True, initial_branch=HUB_BRANCH)
    _apply_fast_forward_only(path)
    return path


def list_workspace_ids() -> list[str]:
    """Return the workspaces this machine hosts, ordered by identifier."""
    root = workspaces_root()
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if (entry / "repository.git" / "HEAD").is_file()
    )


def default_ssh_endpoint() -> str:
    """Return the ``user@host`` this machine suggests to devices."""
    try:
        user = getpass.getuser()
    except OSError:
        # A daemon with no login name still has a usable host part; the user
        # completes the endpoint themselves.
        return socket.gethostname()
    return f"{user}@{socket.gethostname()}"


class HubNotHostedError(RuntimeError):
    """Raised when a hub operation is asked of a machine that hosts no hub."""


def _apply_fast_forward_only(path: Path) -> None:
    """Refuse anything that would drop a commit a device already relies on.

    Applied on every create so a repository restored from a backup, or made by
    hand, cannot quietly accept a force-push.
    """
    repository = Repo(path)
    repository.git.config("receive.denyNonFastForwards", "true")
    repository.git.config("receive.denyDeletes", "true")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
