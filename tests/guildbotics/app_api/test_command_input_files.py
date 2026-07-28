from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from guildbotics.app_api import command_input_files
from guildbotics.app_api.command_input_files import (
    COMMAND_INPUT_DIRECTORY_NAME,
    CommandInputFileStore,
    save_command_input_file,
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


def test_store_uses_a_session_directory_and_removes_it_on_close(tmp_path: Path) -> None:
    store = CommandInputFileStore(temporary_root=tmp_path)
    store.start()

    saved = store.save(_upload(b"png-data"))
    session_directory = saved.parent

    assert session_directory.parent == tmp_path / COMMAND_INPUT_DIRECTORY_NAME
    assert session_directory.stat().st_mode & 0o777 == 0o700
    assert saved.exists()

    store.close()

    assert not session_directory.exists()


def test_store_removes_orphaned_sessions_on_start(tmp_path: Path) -> None:
    root = tmp_path / COMMAND_INPUT_DIRECTORY_NAME
    orphan = root / "session-orphaned"
    orphan.mkdir(parents=True)
    (orphan / "clipboard.png").write_bytes(b"old")

    store = CommandInputFileStore(temporary_root=tmp_path)
    store.start()

    assert not orphan.exists()

    store.close()


def test_store_preserves_another_active_session(tmp_path: Path) -> None:
    first = CommandInputFileStore(temporary_root=tmp_path)
    first.start()
    saved = first.save(_upload(b"active"))

    second = CommandInputFileStore(temporary_root=tmp_path)
    second.start()

    assert saved.exists()

    second.close()
    first.close()
