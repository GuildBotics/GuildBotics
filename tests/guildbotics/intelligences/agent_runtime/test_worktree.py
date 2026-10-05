"""What a command changed in its copy of the working directory, written back
by the host: regular files only, reached without following a link, and never
over a file that changed on the host while the command ran."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from guildbotics.commands.errors import CommandError
from guildbotics.intelligences.agent_environment.spec import (
    WORKTREE_SOURCE,
    WorktreeCopy,
)
from guildbotics.intelligences.agent_runtime import worktree
from guildbotics.runtime.member_invocation import GuestResult
from guildbotics.utils.i18n_tool import t


class _Guest:
    """The command's microVM, as far as its copy goes: GuildBotics' own
    module run on this machine, with the original where the microVM mounts
    it; or, given ``changes``, a microVM that tells those instead."""

    def __init__(self, original: Path, changes: list[dict] | None = None) -> None:
        self.original = original
        self.changes = changes

    def python(self, module: str, *args: str) -> list[str]:
        return [sys.executable, "-m", module, *args]

    async def run_here(self, argv, *, cwd, env, stdin=b"", stdout=None, stdout_limit):
        if self.changes is not None and "changes" in argv:
            out = "".join(json.dumps(each) + "\n" for each in self.changes).encode()
            result = GuestResult(0, out, b"")
        else:
            args = [str(self.original) if a == WORKTREE_SOURCE else a for a in argv]
            done = subprocess.run(args, input=stdin, capture_output=True, check=False)
            result = GuestResult(done.returncode, done.stdout, done.stderr)
        if stdout is None:
            return result
        stdout.write_bytes(result.stdout)
        return GuestResult(result.returncode, b"", result.stderr)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name, content in {
        "a.txt": "a",
        "src/b.py": "b",
        "gone.txt": "g",
        ".git/config": "[core]\n",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    return root


def _identity(path: Path) -> tuple[int, int]:
    """The directory's own (device, inode), as the environment records it."""
    from guildbotics.utils.safe_paths import inspect_host_path

    return inspect_host_path(path, missing=True).identities[-1]


async def _copy(
    monkeypatch, repository: Path, copy_root: Path, changes=None, excluded=()
):
    guest = _Guest(repository, changes)
    monkeypatch.setattr(worktree, "_guest", lambda _: guest)
    return await worktree.copy_worktree(
        object(),
        WorktreeCopy(repository, _identity(repository), tuple(excluded)),
        str(copy_root),
    )


def _write(entry: str, content: bytes, executable: bool = False) -> dict:
    return {
        "path": entry,
        "executable": executable,
        "content": base64.b64encode(content).decode(),
    }


@pytest.mark.asyncio
async def test_what_the_command_changed_in_its_copy_is_written_back(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    copy_root = tmp_path / "copy"
    copied = await _copy(monkeypatch, repository, copy_root)
    (copy_root / "a.txt").write_text("edited", encoding="utf-8")
    (copy_root / "gone.txt").unlink()
    (copy_root / "new" / "deep").mkdir(parents=True)
    (copy_root / "new" / "deep" / "n.txt").write_text("n", encoding="utf-8")
    # What the command writes through the gitfile is no file of the copy.
    (copy_root / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")

    await worktree.write_back(object(), copied)

    assert (repository / "a.txt").read_text(encoding="utf-8") == "edited"
    assert not (repository / "gone.txt").exists()
    assert (repository / "new" / "deep" / "n.txt").read_text(encoding="utf-8") == "n"
    assert (repository / "src" / "b.py").read_text(encoding="utf-8") == "b"
    assert (repository / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


@pytest.mark.asyncio
async def test_a_written_file_keeps_its_mode_but_the_executable_bit(
    tmp_path: Path, monkeypatch, repository: Path, posix_permissions: None
) -> None:
    os.chmod(repository / "a.txt", 0o640)
    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[_write("a.txt", b"run", executable=True), _write("n.sh", b"#!")],
    )

    await worktree.write_back(object(), copied)

    assert (repository / "a.txt").stat().st_mode & 0o777 == 0o750
    assert (repository / "n.sh").stat().st_mode & 0o111 == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        _write(".git/hooks/pre-commit", b"#!/bin/sh"),
        _write("src/.Git/config", b"x"),
        _write("../outside.txt", b"x"),
        _write("/absolute.txt", b"x"),
        _write("src/../a.txt", b"x"),
        _write("src//b.py", b"x"),
        _write("./a.txt", b"x"),
        _write("docs/shared/s.md", b"x"),
        {"path": "a.txt", "deleted": True, "content": ""},
        {"path": "a.txt"},
        {"path": 1},
    ],
    ids=[
        "git",
        "git-other-case",
        "parent",
        "absolute",
        "climbing",
        "empty-part",
        "dot",
        "nested-mount",
        "both",
        "neither",
        "garbled",
    ],
)
async def test_a_change_that_is_no_regular_file_inside_is_refused_whole(
    tmp_path: Path, monkeypatch, repository: Path, change: dict
) -> None:
    """One change the host may not write refuses all of them: nothing of
    what the command changed is written back."""
    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[_write("a.txt", b"edited"), change],
        excluded=("docs/shared",),
    )

    with pytest.raises(CommandError) as refused:
        await worktree.write_back(object(), copied)

    assert str(refused.value).startswith(
        t(
            "intelligences.agent_environment.runtime.worktree_refused",
            path=repository,
            reason="",
        )
    )
    assert (repository / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (tmp_path / "outside.txt").exists()
    assert not (repository / ".git" / "hooks").exists()


@pytest.mark.asyncio
async def test_a_file_changed_twice_is_refused(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[_write("a.txt", b"1"), _write("a.txt", b"2")],
    )

    with pytest.raises(CommandError, match="changed twice"):
        await worktree.write_back(object(), copied)
    assert (repository / "a.txt").read_text(encoding="utf-8") == "a"


@pytest.mark.asyncio
async def test_nothing_is_written_over_a_file_that_changed_while_the_command_ran(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    copy_root = tmp_path / "copy"
    copied = await _copy(monkeypatch, repository, copy_root)
    (copy_root / "a.txt").write_text("command", encoding="utf-8")
    (copy_root / "src" / "b.py").write_text("command", encoding="utf-8")
    (copy_root / "made.txt").write_text("command", encoding="utf-8")
    (repository / "a.txt").write_text("user", encoding="utf-8")
    (repository / "made.txt").write_text("user", encoding="utf-8")

    with pytest.raises(CommandError) as refused:
        await worktree.write_back(object(), copied)

    assert str(refused.value) == t(
        "intelligences.agent_environment.runtime.worktree_conflict",
        path=repository,
        paths="a.txt, made.txt",
    )
    assert (repository / "a.txt").read_text(encoding="utf-8") == "user"
    assert (repository / "src" / "b.py").read_text(encoding="utf-8") == "b"


@pytest.mark.asyncio
async def test_a_link_in_the_working_directory_is_never_followed(
    tmp_path: Path, monkeypatch, repository: Path, symlinks: None
) -> None:
    """A link already there -- a directory or the file itself -- refuses the
    change: the copy left it out, so nothing the command wrote can go
    through it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text("target", encoding="utf-8")
    (repository / "docs").symlink_to(elsewhere, target_is_directory=True)
    (repository / "leaf.txt").symlink_to(elsewhere / "x.md")

    for change in (_write("docs/x.md", b"through"), _write("leaf.txt", b"through")):
        copied = await _copy(
            monkeypatch, repository, tmp_path / "copy", changes=[change]
        )
        with pytest.raises(CommandError):
            await worktree.write_back(object(), copied)

    assert (elsewhere / "x.md").read_text(encoding="utf-8") == "target"


@pytest.mark.asyncio
async def test_a_directory_swapped_for_a_link_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch, repository: Path, symlinks: None
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    copied = await _copy(
        monkeypatch, repository, tmp_path / "copy", changes=[_write("src/b.py", b"x")]
    )
    checked = worktree._unchanged

    def check_then_swap(root, name, files):
        unchanged = checked(root, name, files)
        (repository / "src" / "b.py").unlink()
        (repository / "src").rmdir()
        (repository / "src").symlink_to(elsewhere, target_is_directory=True)
        return unchanged

    monkeypatch.setattr(worktree, "_unchanged", check_then_swap)

    with pytest.raises(CommandError, match="src"):
        await worktree.write_back(object(), copied)
    assert list(elsewhere.iterdir()) == []


@pytest.mark.asyncio
async def test_a_copy_that_cannot_be_made_or_told_fails_the_command(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    with pytest.raises(CommandError) as failed:
        await _copy(monkeypatch, tmp_path / "missing", tmp_path / "copy")
    assert str(failed.value).startswith(
        t("intelligences.agent_environment.runtime.worktree_copy_failed", error="")
    )

    copy_root = tmp_path / "copy"
    copied = await _copy(monkeypatch, repository, copy_root)
    (copy_root / "a.txt").unlink()
    os.mkdir(copy_root / "a.txt")
    with pytest.raises(CommandError) as failed:
        await worktree.write_back(object(), copied)
    assert str(failed.value).startswith(
        t("intelligences.agent_environment.runtime.worktree_changes_failed", error="")
    )


def _leftovers(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if ".guildbotics-" in path.name]


@pytest.mark.asyncio
async def test_a_file_that_cannot_be_written_leaves_nothing_written(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    """Every file is written beside its place before any is put in place."""
    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[_write("a.txt", b"first"), _write("src/b.py", b"second")],
    )
    stage = worktree._stage

    def stage_then_fail(root, path, change):
        if change["path"] == "src/b.py":
            raise OSError("No space left on device")
        return stage(root, path, change)

    monkeypatch.setattr(worktree, "_stage", stage_then_fail)

    with pytest.raises(CommandError, match="No space left"):
        await worktree.write_back(object(), copied)

    assert (repository / "a.txt").read_text(encoding="utf-8") == "a"
    assert _leftovers(repository) == []


@pytest.mark.asyncio
async def test_putting_files_in_place_that_stops_partway_says_what_was_written(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[
            _write("a.txt", b"first"),
            _write("src/b.py", b"second"),
            _write("c.txt", b"third"),
        ],
    )
    put = worktree._put

    def put_then_fail(root, path, temporary):
        if path.as_posix() == "src/b.py":
            raise OSError("Permission denied")
        return put(root, path, temporary)

    monkeypatch.setattr(worktree, "_put", put_then_fail)

    with pytest.raises(CommandError) as stopped:
        await worktree.write_back(object(), copied)

    assert str(stopped.value) == t(
        "intelligences.agent_environment.runtime.worktree_partial",
        path=repository,
        paths="a.txt",
        reason="Permission denied",
    )
    assert (repository / "a.txt").read_text(encoding="utf-8") == "first"
    assert (repository / "src" / "b.py").read_text(encoding="utf-8") == "b"
    assert not (repository / "c.txt").exists()
    assert _leftovers(repository) == []


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("report.txt", True),
        ("aux.c.txt", False),
        ("CON", False),
        ("nul.txt", False),
        ("com1", False),
        ("Lpt9.log", False),
        ("console.txt", True),
        ("trailing.", False),
        ("trailing ", False),
        ("stream:name", False),
    ],
)
def test_a_name_windows_would_take_for_another_is_refused_there(
    name: str, kept: bool
) -> None:
    assert worktree._windows_name(name) is kept


@pytest.mark.asyncio
async def test_a_copy_too_large_to_list_fails_the_command(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    from guildbotics.runtime.member_invocation import GuestProcessError

    class _TooLarge(_Guest):
        async def run_here(self, *_, **__):
            raise GuestProcessError("The command wrote more than 1 bytes of output.")

    monkeypatch.setattr(worktree, "_guest", lambda _: _TooLarge(repository))

    with pytest.raises(CommandError) as failed:
        await worktree.copy_worktree(
            object(),
            WorktreeCopy(repository, _identity(repository)),
            str(tmp_path / "copy"),
        )

    assert str(failed.value) == t(
        "intelligences.agent_environment.runtime.worktree_copy_failed",
        error="The command wrote more than 1 bytes of output.",
    )


@pytest.mark.asyncio
async def test_a_bit_the_host_changed_meanwhile_is_kept_or_refuses_a_change_to_it(
    tmp_path: Path, monkeypatch, repository: Path, posix_permissions: None
) -> None:
    """A command that only edited the file leaves the host's bit as it is
    now; one that changed the bit too finds the host changed it first."""
    os.chmod(repository / "a.txt", 0o644)
    copy_root = tmp_path / "copy"
    copied = await _copy(monkeypatch, repository, copy_root)
    os.chmod(repository / "a.txt", 0o755)
    (copy_root / "a.txt").write_text("edited", encoding="utf-8")

    await worktree.write_back(object(), copied)

    assert (repository / "a.txt").read_text(encoding="utf-8") == "edited"
    assert (repository / "a.txt").stat().st_mode & 0o777 == 0o755

    os.chmod(repository / "a.txt", 0o644)
    copied = await _copy(monkeypatch, repository, copy_root)
    os.chmod(repository / "a.txt", 0o755)
    # The command makes it executable too, as the host already did.
    os.chmod(copy_root / "a.txt", 0o700)
    (copy_root / "a.txt").write_text("again", encoding="utf-8")

    with pytest.raises(CommandError) as refused:
        await worktree.write_back(object(), copied)

    assert str(refused.value) == t(
        "intelligences.agent_environment.runtime.worktree_conflict",
        path=repository,
        paths="a.txt",
    )
    assert (repository / "a.txt").read_text(encoding="utf-8") == "edited"
    assert (repository / "a.txt").stat().st_mode & 0o777 == 0o755


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["same", "nested"])
async def test_a_later_write_back_waits_and_finds_the_earlier(
    tmp_path: Path, monkeypatch, repository: Path, nested: bool
) -> None:
    """Two commands copied the same file, through the same working directory
    or one holding the other's: the one that ends second checks only after
    the first has put its files in place, and refuses."""
    import threading

    inner = repository / "src" if nested else repository
    first = await _copy(monkeypatch, repository, tmp_path / "first")
    second = await _copy(monkeypatch, inner, tmp_path / "second")
    changes = {}
    for name, path, content in (
        ("first", "src/b.py", b"first"),
        ("second", "b.py" if nested else "src/b.py", b"second"),
    ):
        changes[name] = tmp_path / f"{name}.jsonl"
        changes[name].write_text(json.dumps(_write(path, content)) + "\n")
    staging = threading.Event()
    release = threading.Event()
    checked: list[str] = []
    stage, unchanged = worktree._stage, worktree._unchanged

    def stage_slowly(root, path, change):
        if threading.current_thread().name == "first":
            staging.set()
            assert release.wait(10)
        return stage(root, path, change)

    def checking(root, change, copied):
        checked.append(threading.current_thread().name)
        return unchanged(root, change, copied)

    monkeypatch.setattr(worktree, "_stage", stage_slowly)
    monkeypatch.setattr(worktree, "_unchanged", checking)
    errors: dict[str, BaseException] = {}

    def run(name, copied):
        try:
            worktree._write(copied, changes[name])
        except BaseException as exc:
            errors[name] = exc

    threads = [
        threading.Thread(target=run, args=("first", first), name="first"),
        threading.Thread(target=run, args=("second", second), name="second"),
    ]
    threads[0].start()
    assert staging.wait(10)
    threads[1].start()
    threads[1].join(0.5)
    assert checked == ["first"]
    release.set()
    for thread in threads:
        thread.join(10)

    assert "first" not in errors
    assert str(errors["second"]) == t(
        "intelligences.agent_environment.runtime.worktree_conflict",
        path=inner,
        paths="b.py" if nested else "src/b.py",
    )
    assert (repository / "src" / "b.py").read_text(encoding="utf-8") == "first"


@pytest.mark.asyncio
async def test_a_directory_put_in_place_of_the_working_directory_is_never_written(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    """What the command changed belongs to the directory it copied, not to
    one that took its place while it ran."""
    copied = await _copy(
        monkeypatch, repository, tmp_path / "copy", changes=[_write("n.txt", b"x")]
    )
    repository.rename(tmp_path / "moved")
    repository.mkdir()

    with pytest.raises(CommandError, match="replaced"):
        await worktree.write_back(object(), copied)

    assert list(repository.iterdir()) == []


@pytest.mark.asyncio
async def test_two_names_of_one_file_on_the_host_are_refused(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    """Where the host's file system takes ``New.txt`` and ``new.txt`` for one
    file, writing both would silently lose one of them."""
    from guildbotics.utils.safe_paths import host_directory_case_sensitive

    copied = await _copy(
        monkeypatch,
        repository,
        tmp_path / "copy",
        changes=[_write("New.txt", b"1"), _write("new.txt", b"2")],
    )

    if host_directory_case_sensitive(repository):
        await worktree.write_back(object(), copied)
        assert (repository / "New.txt").read_bytes() == b"1"
        assert (repository / "new.txt").read_bytes() == b"2"
    else:
        with pytest.raises(CommandError, match="changed twice"):
            await worktree.write_back(object(), copied)
        assert not (repository / "new.txt").exists()


@pytest.mark.parametrize("case_sensitive", [True, False])
def test_names_one_file_on_the_host_are_told_apart_only_where_they_are_two(
    tmp_path: Path, case_sensitive: bool
) -> None:
    import unicodedata

    changes = tmp_path / "changes.jsonl"
    names = ["New.txt", "new.txt", unicodedata.normalize("NFD", "é.txt"), "é.txt"]
    changes.write_text("".join(json.dumps(_write(n, b"x")) + "\n" for n in names))

    told = worktree._changes(changes, (), case_sensitive)

    if case_sensitive:
        assert [change["path"] for change in told] == names
    else:
        with pytest.raises(ValueError, match="changed twice"):
            list(told)


@pytest.mark.asyncio
async def test_writing_back_leaves_the_command_loop_free(
    tmp_path: Path, monkeypatch, repository: Path
) -> None:
    """Waiting for another write-back blocks a thread of its own, never the
    loop that answers the command's microVM."""
    import asyncio
    import threading

    copied = await _copy(monkeypatch, repository, tmp_path / "copy")
    release = threading.Event()
    monkeypatch.setattr(worktree, "_write", lambda *_: release.wait(10))

    writing = asyncio.create_task(worktree.write_back(object(), copied))
    await asyncio.sleep(0.05)
    assert not writing.done()
    release.set()
    await asyncio.wait_for(writing, 10)


@pytest.mark.parametrize("case_sensitive", [True, False])
@pytest.mark.parametrize(
    "name", ["Docs/shared/x.md", "docs/SHARED/x.md", "DOCS/Shared/x.md"]
)
def test_a_mount_nested_in_the_directory_is_closed_under_every_spelling_of_it(
    tmp_path: Path, case_sensitive: bool, name: str
) -> None:
    """Where the host takes ``Docs/shared`` for the excluded ``docs/shared``,
    a change spelled so would land in that grant -- read-only, perhaps."""
    changes = tmp_path / "changes.jsonl"
    changes.write_text(json.dumps(_write(name, b"x")) + "\n")

    told = worktree._changes(changes, ("docs/shared",), case_sensitive)

    if case_sensitive:
        assert [change["path"] for change in told] == [name]
    else:
        with pytest.raises(ValueError, match="mounted on its own"):
            list(told)
