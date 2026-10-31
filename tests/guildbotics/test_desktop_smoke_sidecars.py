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
    ("runtime", "cli_exit", "screens", "failure"),
    [
        ("available", 0, True, None),
        ("missing", 0, True, "the bundled CLI does not carry"),
        ("available", 7, True, "the bundled CLI does not carry"),
        ("available", 0, False, "the bundled Local API does not serve the screens"),
    ],
)
def test_smoke_checks_the_screens_and_the_cli(
    tmp_path: Path, runtime: str, cli_exit: int, screens: bool, failure: str | None
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(
        ROOT / "scripts/desktop-smoke-sidecars.sh",
        scripts / "desktop-smoke-sidecars.sh",
    )
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
    backend.write_text(
        "#!/bin/sh\n"
        'server="$HOME/.guildbotics/data/server/$GUILDBOTICS_SERVER_PORT"\n'
        'mkdir -p "$server"\n'
        'echo smoke-owner-token >"$server/owner.token"\n'
        "exec sleep 60\n",
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    # The owner token reaches curl only as a header on its standard input. The
    # page names the script it loads, which the sidecar serves when it carries
    # the screens.
    curl.write_text(
        "#!/bin/sh\n"
        'case "$*" in *smoke-owner-token*) exit 1 ;; esac\n'
        "for arg; do\n"
        '  case "$arg" in http*) url=$arg ;; esac\n'
        '  [ "${prev:-}" = -o ] && out=$arg\n'
        "  prev=$arg\n"
        "done\n"
        'case "$url" in\n'
        "  */api/health) exec grep -qx 'Authorization: Bearer smoke-owner-token' ;;\n"
        """  */) echo '<script type="module" src="/assets/main-1.js"></script>' >"$out" ;;\n"""
        f"  */assets/main-1.js) exit {0 if screens else 22} ;;\n"
        "  *) exit 22 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    for executable in (cli, backend, curl):
        executable.chmod(0o755)

    result = subprocess.run(
        ["bash", str(scripts / "desktop-smoke-sidecars.sh")],
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "DESKTOP_TARGET": "x86_64-unknown-linux-gnu",
            "GUILDBOTICS_SERVER_PORT": "8877",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == (0 if failure is None else 1), result.stderr[:1000]
    assert "sidecar health check passed" in result.stdout
    assert failure is None or failure in result.stderr
