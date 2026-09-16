from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from guildbotics.commands import utils as commands_utils
from guildbotics.commands.utils import find_shell, stringify_output


class _SampleModel(BaseModel):
    value: str


def test_stringify_output_handles_model_and_primitives():
    model_output = _SampleModel(value="ok")
    assert "value: ok" in stringify_output(model_output)
    assert stringify_output({"a": 1}) == "a: 1"
    assert stringify_output(["foo", "bar"]) == "foo\nbar"


GIT_ROOT = "C:/Program Files/Git"
WSL_LAUNCHER = "C:/Windows/System32/bash.exe"


def _which(installed: list[str], on_path: list[str]) -> Callable[..., str | None]:
    """``shutil.which`` over a fixed machine: what exists, and what PATH lists."""

    def which(cmd: str, path: str | None = None) -> str | None:
        directories = [Path(path)] if path is not None else map(Path, on_path)
        for directory in directories:
            for executable in installed:
                candidate = Path(executable)
                if candidate.parent == directory and candidate.stem == cmd:
                    return executable
        return None

    return which


def _machine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    windows: bool,
    installed: list[str],
    on_path: list[str],
) -> None:
    # Only this module's view of `shutil` changes; the real `shutil.which` is
    # shared with everything else the test process imports.
    monkeypatch.setattr(commands_utils, "_WINDOWS", windows)
    monkeypatch.setattr(
        commands_utils, "shutil", SimpleNamespace(which=_which(installed, on_path))
    )


@pytest.mark.parametrize("git_directory", ["cmd", "bin", "mingw64/bin"])
def test_windows_uses_the_bash_git_ships_wherever_git_is_found(
    monkeypatch: pytest.MonkeyPatch, git_directory: str
) -> None:
    # The WSL launcher answers to `bash` and comes first on PATH, but it is not
    # a shell GuildBotics can run a script with.
    _machine(
        monkeypatch,
        windows=True,
        installed=[
            f"{GIT_ROOT}/{git_directory}/git.exe",
            f"{GIT_ROOT}/bin/bash.exe",
            WSL_LAUNCHER,
        ],
        on_path=[str(Path(WSL_LAUNCHER).parent), f"{GIT_ROOT}/{git_directory}"],
    )

    assert find_shell() == f"{GIT_ROOT}/bin/bash.exe"


def test_windows_without_git_takes_the_bash_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(
        monkeypatch,
        windows=True,
        installed=["C:/msys64/usr/bin/bash.exe"],
        on_path=["C:/msys64/usr/bin"],
    )

    assert find_shell() == "C:/msys64/usr/bin/bash.exe"


def test_other_platforms_take_the_bash_on_path_without_asking_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _machine(
        monkeypatch,
        windows=False,
        installed=["/opt/git/cmd/git", "/opt/git/bin/bash", "/usr/bin/bash"],
        on_path=["/opt/git/cmd", "/usr/bin"],
    )

    assert find_shell() == "/usr/bin/bash"


@pytest.mark.parametrize("windows", [True, False])
def test_a_machine_with_no_bash_has_no_shell(
    monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    _machine(
        monkeypatch,
        windows=windows,
        installed=[f"{GIT_ROOT}/cmd/git.exe"],
        on_path=[f"{GIT_ROOT}/cmd"],
    )

    assert find_shell() is None
