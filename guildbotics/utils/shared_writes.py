"""The one way GuildBotics writes a workspace's shared files.

Everything under ``<workspace>/.guildbotics/config`` and
``<workspace>/.guildbotics/state`` is written by several writers that do not
know about each other -- the scheduler's member threads, API handlers, the
member commands the backend runs in process, and a ``guildbotics run`` that
executes locally. ``local/`` holds this machine's own state (clones, caches,
the keychain namespace) and is not one of them.

Every write to a shared path goes through the helpers here, and they take the
workspace's shared-write lock, so a write that reads nothing is already
serialized with nothing to declare. A writer deriving what it writes from what
a file already holds uses :func:`update_shared_text` or
:func:`update_shared_json` so that its read is inside the same span.

The helpers live in ``utils`` because every storage layer writes through them,
including ``guildbotics.observability``, which may depend on ``utils`` alone.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from guildbotics.utils.fileio import (
    WorkspaceNotConfiguredError,
    atomic_write_bytes,
    get_identity_root,
    get_workspace_root,
    resolve_from_existing_ancestor,
)
from guildbotics.utils.shared_write_lock import shared_write_lock

#: Directories under ``.guildbotics/`` whose contents the writers share.
SHARED_ROOTS = ("config", "state")

#: Shared records move to a new schema together, without a compatibility path.
#: Every record GuildBotics writes under ``state/`` stamps this, so the format a
#: record was written in is readable from the record itself. It lives here
#: rather than beside any one record because the writers span layers that
#: cannot import each other -- ``observability`` may depend on ``utils`` alone.
SHARED_RECORD_SCHEMA_VERSION = 4


def shared_relative_path(path: Path, workspace_root: Path | None = None) -> str | None:
    """Return the ``.guildbotics``-relative path when ``path`` is shared.

    Args:
        path (Path): An absolute or workspace-relative filesystem path.
        workspace_root (Path | None): The workspace, or None to use the selected one.

    Returns:
        str | None: A POSIX path such as ``state/task-runs/<id>.json``, or
            None when the path is outside the shared roots, is this machine's
            own (``local/``), or no workspace is selected.

    Resolve existing ancestors independently of missing descendants, so a
    concurrent directory creation cannot change the lock's scope.
    """
    try:
        root = resolve_from_existing_ancestor(
            get_workspace_root(workspace_root) / ".guildbotics"
        )
    except WorkspaceNotConfiguredError:
        return None
    try:
        relative = resolve_from_existing_ancestor(path).relative_to(root)
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] not in SHARED_ROOTS:
        return None
    return relative.as_posix()


@contextmanager
def _writing(path: Path, workspace_root: Path | None) -> Iterator[None]:
    """Hold the shared-write lock, but only for a path that is actually shared.

    A file under ``local/`` is this machine's own state, which no writer
    derives from a shared file, so making its writer wait would order it
    against nothing.
    """
    # The identities are shared files of their own, with a lock of their own.
    identity_root = get_identity_root()
    if shared_relative_path(path, identity_root) is not None:
        with shared_write_lock(identity_root):
            yield
        return
    if shared_relative_path(path, workspace_root) is not None:
        with shared_write_lock(workspace_root):
            yield
        return
    yield


def dump_shared_json(payload: Any) -> str:
    """Serialize ``payload`` the one way every writer writes shared JSON.

    Stable key ordering and a trailing newline keep unchanged content
    byte-identical, so an update that changes nothing writes nothing.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_shared_bytes(
    path: Path, data: bytes, workspace_root: Path | None = None
) -> None:
    """Atomically write ``data`` to ``path`` under the shared-write lock.

    A caller deriving what it writes from what a file already holds needs the
    read inside the span too, and reaches for :func:`update_shared_text` or an
    explicit
    :func:`~guildbotics.utils.shared_write_lock.shared_write_lock` instead.
    """
    with _writing(path, workspace_root):
        atomic_write_bytes(path, data)


def write_shared_text(
    path: Path, text: str, workspace_root: Path | None = None
) -> None:
    """Atomically write UTF-8 ``text`` to ``path``."""
    write_shared_bytes(path, text.encode("utf-8"), workspace_root=workspace_root)


def write_shared_json(
    path: Path, payload: Any, workspace_root: Path | None = None
) -> None:
    """Atomically write ``payload`` as stable JSON."""
    write_shared_text(path, dump_shared_json(payload), workspace_root=workspace_root)


def append_shared_text(
    path: Path, text: str, workspace_root: Path | None = None
) -> None:
    """Append UTF-8 ``text`` to ``path``.

    Journals grow by appending rather than by being rewritten, so this is the
    one write that does not replace the file. It is otherwise the same as the
    others: taken under the lock.
    """
    with _writing(path, workspace_root):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


def delete_shared_path(path: Path, workspace_root: Path | None = None) -> None:
    """Delete ``path`` when it exists."""
    with _writing(path, workspace_root):
        path.unlink(missing_ok=True)


def _read_shared_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def update_shared_text(
    path: Path,
    apply: Callable[[str | None], str | None],
    workspace_root: Path | None = None,
) -> str | None:
    """Rewrite ``path`` from what it currently holds, as one locked span.

    The read is inside the lock because that is the whole difficulty: a value
    derived from a file another writer replaces a moment later is written back
    over that writer's version, and nothing records its change as lost.
    Passing the transformation in rather than the finished text is what makes
    the span start at the read instead of leaving each writer to remember to
    say so.

    Content identical to what is already there is not rewritten.

    Args:
        path (Path): The shared file to rewrite.
        apply (Callable[[str | None], str | None]): Receives the current text,
            or None when the file does not exist, and returns the text to
            write -- or None to delete the file.
        workspace_root (Path | None): The workspace, or None for the selected one.

    Returns:
        str | None: The text the file now holds, or None when it was deleted.
    """
    with _writing(path, workspace_root):
        current = _read_shared_text(path)
        updated = apply(current)
        if updated is None:
            delete_shared_path(path, workspace_root=workspace_root)
            return None
        if updated != current:
            write_shared_text(path, updated, workspace_root=workspace_root)
        return updated


def update_shared_json(
    path: Path,
    apply: Callable[[Any | None], Any],
    workspace_root: Path | None = None,
) -> Any:
    """The JSON form of :func:`update_shared_text`.

    Args:
        path (Path): The shared file to rewrite.
        apply (Callable[[Any | None], Any]): Receives the decoded payload, or
            None when the file does not exist, and returns the payload to
            write -- or None to delete the file.
        workspace_root (Path | None): The workspace, or None for the selected one.

    Returns:
        Any: The payload the file now holds, or None when it was deleted.

    Raises:
        json.JSONDecodeError: When the file exists and is not valid JSON. A
            caller that tolerates a damaged file decides that for itself, in
            :func:`update_shared_text`.
    """
    with _writing(path, workspace_root):
        current = _read_shared_text(path)
        written = apply(json.loads(current) if current is not None else None)
        if written is None:
            delete_shared_path(path, workspace_root=workspace_root)
        else:
            updated = dump_shared_json(written)
            if updated != current:
                write_shared_text(path, updated, workspace_root=workspace_root)
    return written
