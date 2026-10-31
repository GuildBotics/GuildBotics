from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.usefixtures("shell_commands")


@pytest.fixture
def dev_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "source checkout"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(
        ROOT / "scripts/desktop-write-dev-binaries.sh",
        scripts / "desktop-write-dev-binaries.sh",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if 'python' in args and '-c' in args:\n"
        "    os.execv(sys.executable, [sys.executable, *args[args.index('python') + 1:]])\n"
        "print(json.dumps({'cwd': os.getcwd(), 'args': args}))\n"
        "sys.exit(int(os.environ.get('CLI_EXIT', '0')))\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("DESKTOP_TARGET", "aarch64-apple-darwin")
    return repo


def _generate(repo: Path) -> Path:
    subprocess.run(
        ["bash", str(repo / "scripts/desktop-write-dev-binaries.sh")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=20,
    )
    return repo / "desktop/src-tauri/binaries/guildbotics"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_generated_cli_preserves_caller_cwd_arguments_and_exit(
    dev_repo: Path, tmp_path: Path, exit_code: int
) -> None:
    programs = _generate(dev_repo)
    caller = tmp_path / "another checkout"
    caller.mkdir()
    args = [
        "member",
        "git",
        "commit",
        "--workspace-mode",
        "current",
        "--repo-path",
        str(caller),
        "--content-file",
        "message with spaces and '$.txt",
    ]
    result = subprocess.run(
        [str(programs / "guildbotics"), *args],
        cwd=caller,
        # The generated wrapper must use the uv it resolved during generation.
        env={**os.environ, "PATH": "/usr/bin:/bin", "CLI_EXIT": str(exit_code)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )

    assert result.returncode == exit_code, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["cwd"]) == caller.resolve()
    assert observed["args"] == [
        "run",
        "--project",
        str(dev_repo.resolve()),
        "--no-sync",
        "guildbotics",
        *args,
    ]


def test_generated_sidecar_still_starts_from_source_checkout(
    dev_repo: Path, tmp_path: Path
) -> None:
    programs = _generate(dev_repo)
    result = subprocess.run(
        # The server takes no arguments: its port is the machine's.
        [str(programs / "guildbotics-app-api")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=20,
    )

    observed = json.loads(result.stdout)
    assert Path(observed["cwd"]) == dev_repo.resolve()
    assert observed["args"] == [
        "run",
        "--no-sync",
        "python",
        "-m",
        "guildbotics.app_api",
    ]


@pytest.mark.parametrize("wrapper", ["CLI_PATH", "SIDECAR_PATH"])
def test_dev_build_id_is_stable_and_changes_with_each_wrapper(
    dev_repo: Path, wrapper: str
) -> None:
    programs = _generate(dev_repo)
    first_id = (programs / "build-id").read_text(encoding="utf-8")
    _generate(dev_repo)
    assert (programs / "build-id").read_text(encoding="utf-8") == first_id

    script = dev_repo / "scripts/desktop-write-dev-binaries.sh"
    source = script.read_text(encoding="utf-8")
    executable = f'chmod +x "${wrapper}"\n'
    assert executable in source
    script.write_text(
        source.replace(
            executable, executable + f'echo "# changed wrapper" >> "${wrapper}"\n'
        ),
        encoding="utf-8",
    )
    _generate(dev_repo)

    assert (programs / "build-id").read_text(encoding="utf-8") != first_id
