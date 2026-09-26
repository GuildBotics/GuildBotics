from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.usefixtures("posix_permissions")
@pytest.mark.parametrize(
    ("runtime", "cli_exit", "expected_exit"),
    [("available", 0, 0), ("missing", 0, 1), ("available", 7, 1)],
)
def test_smoke_consumes_status_output_and_preserves_cli_failure(
    tmp_path: Path, runtime: str, cli_exit: int, expected_exit: int
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("desktop-smoke-sidecars.sh", "desktop-token.sh"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    programs = tmp_path / "desktop/src-tauri/binaries/guildbotics"
    programs.mkdir(parents=True)
    cli = programs / "guildbotics"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if sys.argv[-1] == 'status':\n"
        f"    os.write(1, b'runtime: {runtime}\\n')\n"
        # More than a pipe can buffer: an early reader exit must break the CLI.
        "    for _ in range(256):\n"
        "        os.write(1, b'x' * 4095 + b'\\n')\n"
        f"    sys.exit({cli_exit})\n",
        encoding="utf-8",
    )
    backend = programs / "guildbotics-app-api"
    backend.write_text("#!/bin/sh\nexec sleep 60\n", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for executable in (cli, backend, curl):
        executable.chmod(0o755)

    result = subprocess.run(
        ["bash", str(scripts / "desktop-smoke-sidecars.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "DESKTOP_TARGET": "x86_64-unknown-linux-gnu",
            "GUILDBOTICS_SIDECAR_SMOKE_TOKEN": "smoke-test-token",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == expected_exit, result.stderr[:1000]
    assert "sidecar health check passed" in result.stdout
    assert ("the bundled CLI does not carry" in result.stderr) == bool(expected_exit)
