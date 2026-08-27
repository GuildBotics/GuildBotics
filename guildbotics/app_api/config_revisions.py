"""Refusing a config save that was composed against superseded content.

A config screen is a form over files that another machine can change while it
sits open: synchronization writes an incoming edit straight into the workspace,
and the form knows nothing about it. Saving would then send the whole form
back, quietly replacing the other machine's edit with values read before it
arrived -- and because that is an ordinary local write, nothing in the sync
history records it as a conflict.

So every config read reports the revision of each file it was built from, and
every config save sends those revisions back. This module is the one place
that pairing is turned into an answer the screen can act on: the user's input
is not applied, and the current revisions come back so the screen can reload
rather than retry.

Every endpoint that changes config goes through :func:`apply_config_write`,
including the ones with nothing to compare against. Comparing is optional;
being the only writer while writing is not, and an endpoint that took its own
route would be writing underneath whatever synchronization was doing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from guildbotics.app_api.errors import AppApiError
from guildbotics.utils.fileio import workspace_root_from_config_dir
from guildbotics.workspace.config_repository import (
    ConfigRepository,
    ConfigWriteReceipt,
    StaleConfigWriteError,
)
from guildbotics.workspace.validation import SharedFileInvalidError

#: Reported to the frontend when a save was composed against older content.
CONFIG_CHANGED = "config_changed"


def config_repository(config_dir: Path) -> ConfigRepository:
    """Return the repository for the workspace ``config_dir`` belongs to.

    The directory the endpoint resolved is what decides, so a read and the save
    that follows it describe the same files even when the selected workspace is
    not where the endpoint is writing.
    """
    return ConfigRepository(workspace_root_from_config_dir(config_dir))


def apply_config_write[T](
    config_dir: Path,
    apply: Callable[[], T],
    *,
    expected: Mapping[str, str] | None = None,
    report: Callable[[], dict[str, str]] | None = None,
) -> ConfigWriteReceipt[T]:
    """Run one config change under the workspace's shared-write lock.

    Args:
        config_dir (Path): The config directory being written.
        apply (Callable[[], T]): The change itself.
        expected (Mapping[str, str] | None): Config-relative path to the
            revision the screen last read, or None when the change is not
            composed against earlier content -- first-time setup, or an action
            that replaces one field outright.
        report (Callable[[], dict[str, str]] | None): Reads the revisions to
            answer with, run before the lock is released.

    Raises:
        AppApiError: With :data:`CONFIG_CHANGED` and status 409 when one of the
            files changed. Nothing has been written, and ``context`` carries
            the changed path plus the current revisions.

    Note:
        A lock this could not take raises
        :class:`~guildbotics.utils.shared_write_lock.SharedWriteBusyError`,
        which the application turns into one 503 for every route rather than
        each route naming it.
    """
    try:
        return config_repository(config_dir).write(
            apply, expected=expected, report=report
        )
    except SharedFileInvalidError as exc:
        # The caller named something that is not a config file at all, so this
        # is a malformed request rather than a race with another machine.
        raise AppApiError(
            "config_revision_invalid", reason=str(exc), status_code=400
        ) from exc
    except StaleConfigWriteError as exc:
        raise AppApiError(
            CONFIG_CHANGED,
            context={"path": exc.relative_path, "revisions": exc.revisions},
            status_code=409,
        ) from exc
