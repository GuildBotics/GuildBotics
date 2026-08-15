"""Refusing a config save that was composed against superseded content.

A config screen is a form over files that another machine can change while it
sits open: synchronization writes an incoming edit straight into the workspace,
and the form knows nothing about it. Saving would then send the whole form
back, quietly replacing the other machine's edit with values read before it
arrived -- and because that is an ordinary local write, nothing in the sync
history records it as a conflict.

So every config read reports the revision of each file it was built from, and
every config save sends those revisions back. This module is the one place
that pairing is enforced, and the one place the refusal is turned into an
answer the screen can act on: the user's input is not applied, and the current
revisions come back so the screen can reload rather than retry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from guildbotics.app_api.errors import AppApiError
from guildbotics.utils.fileio import workspace_root_from_config_dir
from guildbotics.workspace.config_repository import (
    ConfigRepository,
    StaleConfigWriteError,
)

#: Reported to the frontend when a save was composed against older content.
CONFIG_CHANGED = "config_changed"


def config_repository(config_dir: Path) -> ConfigRepository:
    """Return the repository for the workspace ``config_dir`` belongs to.

    The directory the endpoint resolved is what decides, so a read and the save
    that follows it describe the same files even when the selected workspace is
    not where the endpoint is writing.
    """
    return ConfigRepository(workspace_root_from_config_dir(config_dir))


@contextmanager
def guarded_config_write(
    config_dir: Path, expected: Mapping[str, str]
) -> Iterator[None]:
    """Apply a config save only while the revisions it was composed against hold.

    Args:
        config_dir (Path): The config directory being written.
        expected (Mapping[str, str]): Config-relative path to the revision the
            screen last read. An empty mapping applies the save unguarded,
            which is what first-time setup does: there is no earlier content to
            be composed against.

    Raises:
        AppApiError: With :data:`CONFIG_CHANGED` and status 409 when one of the
            files changed. Nothing has been written, and ``context`` carries
            the changed path plus the current revisions.
    """
    if not expected:
        yield
        return
    repository = config_repository(config_dir)
    try:
        with repository.guard(expected):
            yield
    except StaleConfigWriteError as exc:
        raise AppApiError(
            CONFIG_CHANGED,
            "This screen was loaded before a more recent change, so nothing "
            "was saved. Reload it and make the change again.",
            context={
                "path": exc.relative_path,
                "revisions": repository.revisions(expected),
            },
            status_code=409,
        ) from exc
