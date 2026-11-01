import os
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from guildbotics.app_api import command_input_files
from guildbotics.app_api.command_input_files import (
    CommandInputFileStore,
    command_cwd,
    save_command_input_file,
)
from guildbotics.app_api.errors import AppApiError


def _upload(content: bytes, content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename="clipboard.png",
        headers=Headers({"content-type": content_type}),
    )


def test_input_store_problem_clears_after_successful_retry(tmp_path, monkeypatch):
    from guildbotics.utils.safe_paths import HostPathPermissionError

    store = CommandInputFileStore(root=tmp_path / "inputs")
    start = store._start
    monkeypatch.setattr(
        store,
        "_start",
        lambda: (_ for _ in ()).throw(HostPathPermissionError(store._root)),
    )
    with pytest.raises(HostPathPermissionError):
        store.start()
    assert store.problem
    monkeypatch.setattr(store, "_start", start)
    try:
        assert store.save(_upload(b"image")).is_file()
        assert store.problem == ""
    finally:
        store.close()


@pytest.mark.parametrize("fault", ["permission", "link"])
def test_unavailable_old_session_lock_does_not_block_new_sessions(
    tmp_path, monkeypatch, symlinks, fault
):
    from guildbotics.utils.safe_paths import HostPathPermissionError

    root = tmp_path / "inputs"
    old = root / "session-old"
    old.mkdir(parents=True)
    lock = old / ".session.lock"
    original = command_input_files.open_host_file
    if fault == "link":
        target = tmp_path / "keep"
        target.write_text("private")
        lock.symlink_to(target)
    else:

        def open_file(path):
            if path == lock:
                raise HostPathPermissionError(lock)
            return original(path)

        monkeypatch.setattr(command_input_files, "open_host_file", open_file)
    store = CommandInputFileStore(root=root)
    try:
        store.start()
        assert store.save(_upload(b"image")).is_file()
        assert old.is_dir()
    finally:
        store.close()


@pytest.mark.parametrize("action", ["save", "close"])
@pytest.mark.windows
def test_session_operations_never_reopen_a_swapped_ancestor(tmp_path, symlinks, action):
    from guildbotics.utils.safe_paths import UnsafePathError

    root = tmp_path / "inputs"
    store = CommandInputFileStore(root=root)
    store.start()
    session = store._session_directory().name
    if os.name == "nt":
        # A live Windows session lock already prevents moving its ancestors.
        # Release this fixture's handle to exercise each operation's own
        # no-follow check as well, without weakening the production lock.
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "original")
        session_lock = store._session_lock
        assert session_lock is not None
        command_input_files.unlock_file(session_lock)
        session_lock.close()
        store._session_lock = None
    root.rename(tmp_path / "original")
    outside = tmp_path / "outside"
    (outside / session).mkdir(parents=True)
    sentinel = outside / session / "keep"
    sentinel.write_bytes(b"private")
    root.symlink_to(outside, target_is_directory=True)
    try:
        if action == "close":
            store.close()
        else:
            with pytest.raises(UnsafePathError):
                store.save(_upload(b"image"))
        assert list((outside / session).iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b"private"
    finally:
        store.close()


def test_concurrent_lazy_uploads_share_one_live_session(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = CommandInputFileStore(root=tmp_path / "inputs")
    barrier = Barrier(8)

    def save(_):
        barrier.wait(timeout=5)
        return store.save(_upload(b"image"))

    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            files = list(workers.map(save, range(8)))
        assert len({p.parent for p in files}) == 1
        assert len(list(files[0].parent.parent.glob("session-*"))) == 1
        assert all(p.read_bytes() == b"image" for p in files)
    finally:
        store.close()
    assert not list((tmp_path / "inputs").glob("session-*"))


def test_save_command_input_file_uses_private_random_path(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    directory.mkdir(mode=0o755)

    saved = save_command_input_file(directory, _upload(b"png-data"))

    assert saved.parent == directory
    assert saved.suffix == ".png"
    assert saved.read_bytes() == b"png-data"
    assert not list(saved.parent.glob(".*.upload"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX OS aliases")
def test_command_cwd_uses_the_normalized_os_name(fake_platform):
    from guildbotics.utils import safe_paths

    fake_platform(safe_paths, "darwin")
    assert command_cwd(Path("/tmp/work")) == Path("/private/tmp/work")


def test_command_input_files_are_readable_only_by_their_owner(
    tmp_path: Path, posix_permissions
) -> None:
    """The session directory and everything put in it stay private."""
    directory = tmp_path / "session"
    directory.mkdir(mode=0o755)

    saved = save_command_input_file(directory, _upload(b"png-data"))

    assert directory.stat().st_mode & 0o777 == 0o700
    assert saved.stat().st_mode & 0o777 == 0o600

    store = CommandInputFileStore(root=tmp_path / "inputs")
    store.start()
    try:
        assert store.save(_upload(b"png-data")).parent.stat().st_mode & 0o777 == 0o700
    finally:
        store.close()


def test_save_command_input_file_does_not_recreate_missing_session(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "missing"

    with pytest.raises(ValueError):
        save_command_input_file(directory, _upload(b"png-data"))

    assert not directory.exists()


@pytest.mark.parametrize(
    ("filename", "suffix"),
    [
        ("report.pdf", "-report.pdf"),
        ("C:\\Users\\me\\notes.md", "-notes.md"),
        ("../../escape.txt", "-escape.txt"),
        ("", "-file"),
        ("あ" * 200 + ".pdf", "-" + "あ" * 56 + ".pdf"),
    ],
)
def test_an_uploaded_file_keeps_only_its_own_name(
    tmp_path: Path, filename: str, suffix: str
) -> None:
    upload = UploadFile(
        BytesIO(b"content"),
        filename=filename,
        headers=Headers({"content-type": "application/pdf"}),
    )

    saved = save_command_input_file(tmp_path, upload)

    assert saved.parent == tmp_path and saved.name.endswith(suffix)
    assert saved.read_bytes() == b"content"


@pytest.mark.windows
def test_save_command_input_file_removes_partial_oversized_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_input_files, "MAX_COMMAND_INPUT_FILE_BYTES", 4)

    with pytest.raises(ValueError, match="too large"):
        save_command_input_file(tmp_path, _upload(b"12345"))

    assert list(tmp_path.iterdir()) == []


def test_store_lives_where_no_microvm_writes_by_default(tmp_path: Path) -> None:
    """Under the machine-local state root: no grant may open it, and its
    session directory is mounted read-only into the commands it runs."""
    home = tmp_path / "home"

    store = CommandInputFileStore()
    assert store.directory is None
    store.start()
    try:
        saved = store.save(_upload(b"png-data"))
        assert saved.parent == store.directory
        assert saved.is_relative_to(home / ".guildbotics/data/command_inputs")
        assert not saved.is_relative_to(home / "Documents")
    finally:
        store.close()


@pytest.mark.windows
def test_store_uses_a_session_directory_and_removes_it_on_close(tmp_path: Path) -> None:
    store = CommandInputFileStore(root=tmp_path / "inputs")
    store.start()

    saved = store.save(_upload(b"png-data"))
    session_directory = saved.parent

    assert session_directory.parent == tmp_path / "inputs"
    assert saved.exists()

    store.close()

    assert not session_directory.exists()


@pytest.mark.windows
def test_store_removes_orphaned_sessions_on_start(tmp_path: Path) -> None:
    orphan = tmp_path / "inputs/session-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "clipboard.png").write_bytes(b"old")

    store = CommandInputFileStore(root=tmp_path / "inputs")
    store.start()

    assert not orphan.exists()

    store.close()


@pytest.mark.windows
def test_store_preserves_another_active_session(tmp_path: Path) -> None:
    first = CommandInputFileStore(root=tmp_path / "inputs")
    first.start()
    saved = first.save(_upload(b"active"))

    second = CommandInputFileStore(root=tmp_path / "inputs")
    second.start()

    assert saved.exists()

    second.close()
    first.close()


def test_session_creation_refuses_an_exchange_name_swapped_after_validation(
    tmp_path, monkeypatch, symlinks
):
    from guildbotics.utils.safe_paths import UnsafePathError

    root = tmp_path / "inputs"
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    inspect = command_input_files.inspect_host_path

    def swap(path, *args, **kwargs):
        checked = inspect(path, *args, **kwargs)
        root.rename(tmp_path / "original")
        root.symlink_to(outside, target_is_directory=True)
        return checked

    monkeypatch.setattr(command_input_files, "inspect_host_path", swap)
    with pytest.raises((UnsafePathError, OSError)):
        CommandInputFileStore(root=root).start()
    assert not list(outside.iterdir())
    if os.name != "nt":
        assert outside.stat().st_mode & 0o777 == 0o755


def test_the_working_directory_expands_the_home_and_refuses_relative_paths(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    # No shell sits in front of the Desktop's field, so `~` is expanded here.
    assert command_cwd(None) is None
    assert command_cwd(Path("~/gb-test/clone")) == home / "gb-test/clone"
    assert command_cwd(home / "x") == home / "x"
    # A relative path has nothing the user can see to resolve against.
    with pytest.raises(AppApiError) as refused:
        command_cwd(Path("gb-test/clone"))
    assert refused.value.code == "command_cwd_not_absolute"
    assert str(Path("gb-test/clone")) in refused.value.message
