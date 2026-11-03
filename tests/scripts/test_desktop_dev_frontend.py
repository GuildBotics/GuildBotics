"""Exercise workspace installation and Vite launch without real npm or servers."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/desktop-dev-frontend.sh"
STUB = """\
import json, os, sys
from pathlib import Path
with Path(os.environ['NPM_CALLS']).open('a') as stream:
    stream.write(json.dumps([str(Path.cwd()), sys.argv[1:],
                             os.environ['GUILDBOTICS_SERVER_PORT'], os.getpid()]) + '\\n')
if sys.argv[1:] == ['ci']:
    sys.exit(int(os.environ.get('CI_EXIT', '0')))
sys.exit(7)
"""


@pytest.mark.parametrize(
    "state", ["missing-directory", "missing-marker", "current", "changed-lock"]
)
@pytest.mark.parametrize("ci_exit", [0, 23])
def test_frontend_uses_the_root_lock_and_preserves_launch_environment(
    tmp_path, state, ci_exit
):
    repo = tmp_path / "checkout with spaces"
    (repo / "scripts").mkdir(parents=True)
    (repo / "desktop").mkdir()
    script = repo / "scripts/desktop-dev-frontend.sh"
    shutil.copyfile(SCRIPT, script)
    lock = repo / "package-lock.json"
    lock.write_text("{}")
    os.utime(lock, (1000, 1000))
    modules = repo / "node_modules"
    if state != "missing-directory":
        modules.mkdir()
    if state in ("current", "changed-lock"):
        marker = modules / ".package-lock.json"
        marker.write_text("{}")
        os.utime(marker, (2000, 2000))
    if state == "changed-lock":
        os.utime(lock, (3000, 3000))
    tools = tmp_path / "tools"
    tools.mkdir()
    npm = tools / "npm"
    npm.write_text(f"#!{sys.executable}\n{STUB}")
    npm.chmod(0o755)
    calls = tmp_path / "calls"

    with subprocess.Popen(
        ["bash", str(script)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
            "NPM_CALLS": str(calls),
            "CI_EXIT": str(ci_exit),
            "GUILDBOTICS_SERVER_PORT": "9988",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        _, stderr = process.communicate(timeout=15)
    installs = state != "current"
    failed = installs and ci_exit != 0
    assert process.returncode == (ci_exit if failed else 7), stderr
    expected = [["ci"]] if installs else []
    if not failed:
        expected.append(["run", "dev", "--workspace", "desktop"])
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [call[:3] for call in recorded] == [
        [str(repo), args, "9988"] for args in expected
    ]
    if not failed:
        assert recorded[-1][3] == process.pid
