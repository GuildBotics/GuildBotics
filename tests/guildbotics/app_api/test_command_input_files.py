from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from guildbotics.app_api import command_input_files
from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.command_input_files import (
    command_cwd,
    CommandInputFileStore,
    GrantSuggestion,
    copy_command_input_file,
    describe_command_input_paths,
    save_command_input_file,
)
from guildbotics.intelligences.agent_environment.contract import (
    DocumentGrant,
    LocalGrants,
    SharedGrants,
)


def _upload(content: bytes, content_type: str = "image/png") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename="clipboard.png",
        headers=Headers({"content-type": content_type}),
    )


def test_save_command_input_file_uses_private_random_path(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    directory.mkdir(mode=0o755)

    saved = save_command_input_file(directory, _upload(b"png-data"))

    assert saved.parent == directory
    assert directory.stat().st_mode & 0o777 == 0o700
    assert saved.suffix == ".png"
    assert saved.read_bytes() == b"png-data"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert not list(saved.parent.glob(".*.upload"))


def test_save_command_input_file_does_not_recreate_missing_session(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
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
    assert copied.stat().st_mode & 0o777 == 0o600
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
    with pytest.raises(ValueError, match="is not a file"):
        copy_command_input_file(directory, tmp_path / "missing.txt")
    with pytest.raises(ValueError, match="too large"):
        copy_command_input_file(directory, big)

    assert list(directory.iterdir()) == []


def test_store_lives_in_the_exchange_tmp_directory_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    store = CommandInputFileStore()
    store.start()
    try:
        saved = store.save(_upload(b"png-data"))
        # Under the default document grant, so a turn reaches it at this path.
        assert saved.is_relative_to(home / "Documents/GuildBotics/tmp")
    finally:
        store.close()


def test_store_uses_a_session_directory_and_removes_it_on_close(tmp_path: Path) -> None:
    store = CommandInputFileStore(root=tmp_path)
    store.start()

    saved = store.save(_upload(b"png-data"))
    source = tmp_path / "notes.md"
    source.write_text("hello", encoding="utf-8")
    copied = store.copy(source)
    session_directory = saved.parent

    assert session_directory.parent == tmp_path
    assert session_directory.stat().st_mode & 0o777 == 0o700
    assert saved.exists()
    assert copied.parent == session_directory

    store.close()

    assert not session_directory.exists()
    assert source.exists()


def test_store_removes_orphaned_sessions_on_start(tmp_path: Path) -> None:
    orphan = tmp_path / "session-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "clipboard.png").write_bytes(b"old")

    store = CommandInputFileStore(root=tmp_path)
    store.start()

    assert not orphan.exists()

    store.close()


def test_store_preserves_another_active_session(tmp_path: Path) -> None:
    first = CommandInputFileStore(root=tmp_path)
    first.start()
    saved = first.save(_upload(b"active"))

    second = CommandInputFileStore(root=tmp_path)
    second.start()

    assert saved.exists()

    second.close()
    first.close()


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
    assert "gb-test/clone" in refused.value.message


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
    home.mkdir()
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
