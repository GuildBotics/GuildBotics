import os
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from guildbotics.app_api import command_input_files
from guildbotics.app_api.command_input_files import (
    CommandInputFileStore,
    GrantSuggestion,
    command_cwd,
    copy_command_input_file,
    describe_command_input_paths,
    save_command_input_file,
)
from guildbotics.app_api.errors import AppApiError
from guildbotics.intelligences.agent_environment.contract import (
    SENSITIVE_HOME_DIRECTORIES,
    DeniedPath,
    DocumentGrant,
    LocalGrants,
    ResolvedAccess,
    ResolvedGrant,
    SharedGrants,
)


@pytest.mark.parametrize(
    "protected", [*SENSITIVE_HOME_DIRECTORIES, "workspace", "local"]
)
@pytest.mark.parametrize("shape", ["direct", "leaf", "ancestor"])
def test_copy_and_preview_never_expose_any_protected_source(
    tmp_path, monkeypatch, symlinks, protected, shape
):
    from guildbotics.utils.safe_paths import UnsafePathError

    home = Path.home()
    root = (
        home / protected
        if protected not in {"workspace", "local"}
        else tmp_path / protected
    )
    root.mkdir(parents=True, exist_ok=True)
    secret = root / "private.txt"
    secret.write_text("private")
    writable = tmp_path / "writable"
    writable.mkdir()
    closed = DeniedPath(
        root, "credentials" if protected in SENSITIVE_HOME_DIRECTORIES else protected
    )
    monkeypatch.setattr(command_input_files, "protected_paths", lambda: (closed,))
    monkeypatch.setattr(
        command_input_files,
        "resolve_access",
        lambda *a, **k: ResolvedAccess(
            paths=(ResolvedGrant(writable, "read_write", str(writable)),),
            denied=(closed,),
        ),
    )
    source = secret
    if shape == "leaf":
        source = writable / "chosen.txt"
        source.symlink_to(secret)
    elif shape == "ancestor":
        link = writable / "link"
        link.symlink_to(root, target_is_directory=True)
        source = link / secret.name
    destination = tmp_path / "session"
    destination.mkdir()
    with pytest.raises(UnsafePathError):
        copy_command_input_file(destination, source)
    preview = describe_command_input_paths([source], cwd=writable)[0]
    assert not preview.reachable
    assert preview.grant is None
    assert preview.problem
    assert list(destination.iterdir()) == []
    assert secret.read_text() == "private"


@pytest.mark.parametrize("cwd", [None, "new"])
def test_old_turn_link_is_refused_after_switching_working_directory(
    tmp_path, symlinks, cwd, monkeypatch
):
    from guildbotics.utils.safe_paths import UnsafePathError

    old = tmp_path / "old"
    old.mkdir()
    private = tmp_path / "private-documents"
    private.mkdir()
    (private / "report").write_text("private")
    (old / "link").symlink_to(private, target_is_directory=True)
    new = tmp_path / "new"
    new.mkdir()
    monkeypatch.chdir(new)
    with pytest.raises(UnsafePathError):
        copy_command_input_file(tmp_path, old / "link/report")
    assert describe_command_input_paths(
        [old / "link/report"], cwd=new if cwd else None
    )[0].problem
    assert not list(tmp_path.glob("*-report"))


def test_copy_keeps_selected_basename_and_never_calls_path_resolve(
    tmp_path, monkeypatch
):
    source = tmp_path / "selected.txt"
    source.write_text("public")
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("drive spelling must survive")
        ),
    )
    monkeypatch.setattr(command_input_files, "protected_paths", lambda: ())
    copied = copy_command_input_file(tmp_path, source)
    assert copied.name.endswith("-selected.txt")
    assert copied.read_text() == "public"


def _upload(content: bytes, content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename="clipboard.png",
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.parametrize("language", ["en", "ja"])
def test_copy_source_disappearance_is_invalid_input_and_cleans_partial_copy(
    tmp_path, monkeypatch, language
):
    from fastapi.testclient import TestClient

    from guildbotics.app_api.api import create_app
    from guildbotics.utils import i18n_tool

    source = tmp_path / "report"
    source.write_text("public")
    original = command_input_files.consume_host_file

    def disappear(path, consume):
        path.unlink()
        original(path, consume)

    monkeypatch.setattr(command_input_files, "consume_host_file", disappear)
    store = CommandInputFileStore(root=tmp_path / "inputs")
    client = TestClient(
        create_app(session_token="secret", command_input_file_store=store)
    )
    previous = i18n_tool.get_language()
    i18n_tool.set_language(language)
    try:
        response = client.post(
            "/commands/input-files/copy",
            headers={"X-GuildBotics-Session-Token": "secret"},
            json={"path": str(source)},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "command_input_file_invalid"
        assert (
            i18n_tool.t("safe_paths.file_missing", path=source)
            in response.json()["message"]
        )
        assert not list(store._directory.glob("*report*"))
    finally:
        i18n_tool.set_language(previous)
        store.close()


def test_user_copy_accepts_linked_sources_and_streams_a_growing_file(
    tmp_path, monkeypatch, symlinks
):
    original = tmp_path / "original"
    original.mkdir()
    source = original / "report.txt"
    source.write_bytes(b"public")
    link = tmp_path / "Dropbox"
    link.symlink_to(original, target_is_directory=True)
    destination = tmp_path / "session"
    destination.mkdir()
    assert (
        copy_command_input_file(destination, link / source.name).read_bytes()
        == b"public"
    )
    before = set(destination.iterdir())
    monkeypatch.setattr(command_input_files, "MAX_COMMAND_INPUT_FILE_BYTES", 8)
    calls = []

    def consume(path, chunk):
        assert path == source
        for content in (b"1234", b"5678", b"9", b"never"):
            calls.append(content)
            chunk(content)

    monkeypatch.setattr(command_input_files, "consume_host_file", consume)
    with pytest.raises(ValueError, match="too large"):
        copy_command_input_file(destination, link / source.name)
    assert calls == [b"1234", b"5678", b"9"]
    assert set(destination.iterdir()) == before


@pytest.mark.parametrize("stage", ["lstat", "stat"])
def test_copy_permission_refusal_preserves_absolute_path(tmp_path, monkeypatch, stage):
    from guildbotics.utils.safe_paths import HostPathPermissionError

    source = tmp_path / "report"
    source.write_text("public")
    original = getattr(Path, stage)

    def refuse(self, *args, **kwargs):
        if self == source:
            raise PermissionError(13, "denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, stage, refuse)
    with pytest.raises(HostPathPermissionError) as failure:
        copy_command_input_file(tmp_path, source)
    assert failure.value.filename == str(source)


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


@pytest.mark.parametrize("action", ["save", "copy", "close"])
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
    source = tmp_path / "input"
    source.write_bytes(b"input")
    try:
        if action == "close":
            store.close()
        else:
            with pytest.raises(UnsafePathError):
                store.save(_upload(b"image")) if action == "save" else store.copy(
                    source
                )
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
def test_command_cwd_and_input_paths_use_the_normalized_os_name(
    tmp_path, monkeypatch, fake_platform
):
    from guildbotics.utils import safe_paths

    fake_platform(safe_paths, "darwin")
    assert command_cwd(Path("/tmp/work")) == Path("/private/tmp/work")
    described = describe_command_input_paths([Path("/tmp/work/file")])
    assert described[0].guest_path == "/private/tmp/work/file"


def test_input_preview_classifies_symlinks_without_failing_other_paths(
    tmp_path, monkeypatch, symlinks
):
    from guildbotics.intelligences.agent_environment.contract import (
        ResolvedAccess,
        ResolvedGrant,
    )

    original = tmp_path / "file"
    original.write_bytes(b"public")
    link = tmp_path / "link"
    link.symlink_to(original)
    access = ResolvedAccess(paths=(ResolvedGrant(tmp_path, "read", str(tmp_path)),))
    monkeypatch.setattr(command_input_files, "resolve_access", lambda *a, **k: access)
    described = describe_command_input_paths([original, link])
    assert [p.reachable for p in described] == [True, False]


def test_command_input_files_are_readable_only_by_their_owner(
    tmp_path: Path, posix_permissions
) -> None:
    """The session directory and everything put in it stay private."""
    directory = tmp_path / "session"
    directory.mkdir(mode=0o755)
    source = tmp_path / "report.docx"
    source.write_bytes(b"doc")

    saved = save_command_input_file(directory, _upload(b"png-data"))
    copied = copy_command_input_file(directory, source)

    assert directory.stat().st_mode & 0o777 == 0o700
    assert saved.stat().st_mode & 0o777 == 0o600
    assert copied.stat().st_mode & 0o777 == 0o600

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


def test_save_command_input_file_rejects_unsupported_content_type(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="Only PNG"):
        save_command_input_file(tmp_path, _upload(b"pdf", "application/pdf"))

    assert list(tmp_path.iterdir()) == []


def test_save_command_input_file_removes_partial_oversized_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_input_files, "MAX_COMMAND_INPUT_FILE_BYTES", 4)

    with pytest.raises(ValueError, match="too large"):
        save_command_input_file(tmp_path, _upload(b"12345"))

    assert list(tmp_path.iterdir()) == []


def test_copy_command_input_file_keeps_the_name_behind_a_random_prefix(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "session"
    directory.mkdir()
    source = tmp_path / "report.docx"
    source.write_bytes(b"doc")

    copied = copy_command_input_file(directory, source)
    again = copy_command_input_file(directory, source)

    assert copied.parent == directory
    assert copied.name.endswith("-report.docx")
    assert copied.read_bytes() == b"doc"
    assert again != copied
    assert source.read_bytes() == b"doc"


def test_copy_command_input_file_refuses_directories_and_large_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "session"
    directory.mkdir()
    folder = tmp_path / "folder"
    folder.mkdir()
    big = tmp_path / "big.bin"
    big.write_bytes(b"12345")
    monkeypatch.setattr(command_input_files, "MAX_COMMAND_INPUT_FILE_BYTES", 4)

    with pytest.raises(ValueError, match="is not a file"):
        copy_command_input_file(directory, folder)
    with pytest.raises(ValueError) as error:
        copy_command_input_file(directory, tmp_path / "missing.txt")
    assert str(error.value) == command_input_files.t(
        "safe_paths.file_missing", path=tmp_path / "missing.txt"
    )
    with pytest.raises(ValueError, match="too large"):
        copy_command_input_file(directory, big)

    assert list(directory.iterdir()) == []


def test_store_lives_where_no_microvm_writes_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the machine-local state root: no grant may open it, and its
    session directory is mounted read-only into the commands it runs."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

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


def test_store_uses_a_session_directory_and_removes_it_on_close(tmp_path: Path) -> None:
    store = CommandInputFileStore(root=tmp_path / "inputs")
    store.start()

    saved = store.save(_upload(b"png-data"))
    source = tmp_path / "notes.md"
    source.write_text("hello", encoding="utf-8")
    copied = store.copy(source)
    session_directory = saved.parent

    assert session_directory.parent == tmp_path / "inputs"
    assert saved.exists()
    assert copied.parent == session_directory

    store.close()

    assert not session_directory.exists()
    assert source.exists()


def test_store_removes_orphaned_sessions_on_start(tmp_path: Path) -> None:
    orphan = tmp_path / "inputs/session-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "clipboard.png").write_bytes(b"old")

    store = CommandInputFileStore(root=tmp_path / "inputs")
    store.start()

    assert not orphan.exists()

    store.close()


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    # No shell sits in front of the Desktop's field, so `~` is expanded here.
    assert command_cwd(None) is None
    assert command_cwd(Path("~/gb-test/clone")) == home / "gb-test/clone"
    assert command_cwd(home / "x") == home / "x"
    # A relative path has nothing the user can see to resolve against.
    with pytest.raises(AppApiError) as refused:
        command_cwd(Path("gb-test/clone"))
    assert refused.value.code == "command_cwd_not_absolute"
    assert str(Path("gb-test/clone")) in refused.value.message


def test_describe_command_input_paths_answers_as_the_turn_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "Documents/GuildBotics/tmp").mkdir(parents=True)
    (home / "Desktop").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The exchange directory needs no entry: GuildBotics grants it itself.
    monkeypatch.setattr(command_input_files, "load_shared_grants", SharedGrants)
    monkeypatch.setattr(command_input_files, "load_local_grants", LocalGrants)
    pasted = home / "Documents/GuildBotics/tmp/a.png"
    pasted.write_bytes(b"x")
    shot = home / "Desktop/shot.png"
    shot.write_bytes(b"x")
    cwd = tmp_path / "clone"
    (cwd / "src").mkdir(parents=True)
    outside = tmp_path / "volume/report.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    at_home = home / "notes.md"
    at_home.write_text("x", encoding="utf-8")

    # The field carries the environment's own spelling of a path.
    monkeypatch.setattr(
        command_input_files, "guest_path", lambda path: f"/guest{path.as_posix()}"
    )
    described = describe_command_input_paths(
        [
            pasted,
            shot,
            home / "Desktop",
            cwd / "src",
            home / "gone.txt",
            outside,
            at_home,
        ],
        cwd,
    )

    # An unreachable path names the grant that would open it: the file's
    # directory (or the directory itself), as a document grant under the
    # home and a device path elsewhere; the home itself cannot be granted.
    assert all(d.guest_path == f"/guest{d.path.as_posix()}" for d in described)
    assert [(d.path, d.kind, d.reachable, d.grant) for d in described] == [
        (pasted, "file", True, None),
        (shot, "file", False, GrantSuggestion("document", "Desktop")),
        (home / "Desktop", "directory", False, GrantSuggestion("document", "Desktop")),
        (cwd / "src", "directory", True, None),
        (home / "gone.txt", "missing", False, None),
        (
            outside,
            "file",
            False,
            GrantSuggestion("device", str((tmp_path / "volume").resolve())),
        ),
        (at_home, "file", False, None),
    ]


def test_describe_command_input_paths_reaches_nothing_when_grants_are_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # A grant that names a file cannot be resolved.
    (home / "notes").write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(
        command_input_files,
        "load_shared_grants",
        lambda: SharedGrants(documents=[DocumentGrant(path="notes", access="read")]),
    )
    monkeypatch.setattr(command_input_files, "load_local_grants", LocalGrants)

    # The turn would refuse to start over these grants, so nothing is reachable.
    assert [d.reachable for d in describe_command_input_paths([home / "notes"])] == [
        False
    ]
