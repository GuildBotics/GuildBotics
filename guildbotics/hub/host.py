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
from pathlib import Path

from git import GitCommandError, Repo
from pydantic import BaseModel, ConfigDict, Field

from guildbotics.utils.advisory_lock import held_lock
from guildbotics.utils.fileio import atomic_write_text, get_machine_root
from guildbotics.utils.timestamps import utc_now_iso
from guildbotics.utils.workspace_sync_port import dump_shared_json

#: The branch every workspace repository shares. Users get no branch controls.
HUB_BRANCH = "main"
#: ``hub.json`` is machine-local, so it has a version of its own rather than
#: the shared-record generation that travels between devices.
HUB_SCHEMA_VERSION = 1

# The hook is deliberately a shell one-liner with no payload parsing. A hook
# failure must never turn a successful fast-forward into a failed push; the
# next watch poll and the regular sync fallback remain authoritative.
POST_RECEIVE_HOOK = """#!/bin/sh
touch "$(dirname "$0")/../../head-updated" 2>/dev/null || :
exit 0
"""


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


def require_uuid(value: str, label: str) -> str:
    """Return a value only when it is a canonical UUID."""
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidWorkspaceIdError(f"{label} must be a canonical UUID.") from exc
    if canonical != value:
        raise InvalidWorkspaceIdError(f"{label} must be a canonical UUID.")
    return canonical


def require_workspace_id(workspace_id: str) -> str:
    """Return ``workspace_id`` once it is certainly a workspace identifier.

    It arrives from a remote command line and becomes both a directory name and
    an argument to another machine's shell, so anything that could name
    something else is refused first. Only the canonical form is accepted:
    ``urn:uuid:``, braces, and undashed hex all parse as the same UUID, and
    letting them through would spread one workspace over several directories --
    and, on Windows, produce a directory name the filesystem rejects.

    Raises:
        InvalidWorkspaceIdError: When ``workspace_id`` is not a canonical UUID.
    """
    try:
        return require_uuid(workspace_id, "workspace_id")
    except InvalidWorkspaceIdError as exc:
        raise InvalidWorkspaceIdError(
            f"{workspace_id!r} is not a workspace identifier."
        ) from exc


def workspace_repository_path(workspace_id: str) -> Path:
    """Return the bare repository directory for one workspace."""
    return workspaces_root() / require_workspace_id(workspace_id) / "repository.git"


def read_hub() -> HubSettings | None:
    """Return this machine's hub settings, or None when it hosts no hub."""
    path = hub_settings_path()
    if not path.is_file():
        return None
    return HubSettings.model_validate_json(path.read_text(encoding="utf-8"))


def create_hub() -> HubSettings:
    """Make this machine a hub, keeping the settings of one it already hosts.

    Recreating the settings would mint a second hub identifier for what is
    still the same hub, so an existing one is returned untouched. First use is
    serialized for the same reason identity creation is: two concurrent starts
    must not each write a hub identifier and hand one caller back an
    identifier that no longer exists on disk.
    """
    existing = read_hub()
    if existing is not None:
        return existing
    hub_root().mkdir(parents=True, exist_ok=True)
    with held_lock(hub_root() / "hub-create.lock"):
        existing = read_hub()
        if existing is not None:
            return existing
        settings = HubSettings(
            hub_id=str(uuid.uuid4()),
            created_at=utc_now_iso(),
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
        InvalidWorkspaceIdError: When ``workspace_id`` names something else.
        HubError: When the repository could not be created.
    """
    if read_hub() is None:
        raise HubNotHostedError(
            "This machine is not a hub. Make it one before registering a workspace."
        )
    path = workspace_repository_path(workspace_id)
    try:
        if not (path / "HEAD").is_file():
            path.mkdir(parents=True, exist_ok=True)
            Repo.init(path, bare=True, initial_branch=HUB_BRANCH)
        _apply_fast_forward_only(path)
        _apply_post_receive_hook(path)
    except (GitCommandError, OSError) as exc:
        raise HubError(
            f"The repository for {workspace_id} was not created: {exc}"
        ) from exc
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
    """Return this machine's ``user@host``.

    A hub offers it to devices as the address to connect to, and a device
    writes it into the comment of its own key so that one line of a hub's
    ``authorized_keys`` can be told from another. Neither use can verify it:
    only the user knows which name their machines resolve to.
    """
    try:
        user = getpass.getuser()
    except OSError:
        # A daemon with no login name still has a usable host part; the user
        # completes the endpoint themselves.
        return socket.gethostname()
    return f"{user}@{socket.gethostname()}"


class HubError(RuntimeError):
    """Raised when a hub operation on this machine could not be completed."""


class HubNotHostedError(HubError):
    """Raised when a hub operation is asked of a machine that hosts no hub."""


class InvalidWorkspaceIdError(HubError, ValueError):
    """Raised when text that should identify a workspace does not."""


def _apply_fast_forward_only(path: Path) -> None:
    """Refuse anything that would drop a commit a device already relies on.

    Applied on every create so a repository restored from a backup, or made by
    hand, cannot quietly accept a force-push.
    """
    repository = Repo(path)
    repository.git.config("receive.denyNonFastForwards", "true")
    repository.git.config("receive.denyDeletes", "true")


def _apply_post_receive_hook(path: Path) -> None:
    """Install the workspace head marker hook on every repository creation."""
    hook = path / "hooks" / "post-receive"
    atomic_write_text(hook, POST_RECEIVE_HOOK)
    hook.chmod(0o755)
