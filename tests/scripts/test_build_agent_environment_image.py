"""The developer image workflow uses the real CLI grammar without Docker."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("override", [False, True])
def test_managed_cli_path_with_spaces_is_one_argument(tmp_path, override):
    home = tmp_path / "a user's home"
    binary = home / ".guildbotics/bin/guildbotics"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "with open(os.environ['CLI_LOG'],'a') as log: log.write(json.dumps(sys.argv[1:])+'\\n')\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv[1:3] == ['image','inspect']: print('sha256:'+'a'*64)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    cpu = tools / "uname"
    cpu.write_text("#!/bin/sh\necho arm64\n", encoding="utf-8")
    cpu.chmod(0o755)
    log = tmp_path / "cli.jsonl"
    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{tools}{os.pathsep}{os.environ['PATH']}",
        CLI_LOG=str(log),
        ARCHIVE_DIR=str(tmp_path / "archive files"),
        PLATFORMS="linux/arm64,linux/amd64",
    )
    if override:
        env["GUILDBOTICS_CLI"] = str(binary)
    else:
        env.pop("GUILDBOTICS_CLI", None)
    env.pop("GUILDBOTICS_CLI_ARGS", None)
    script = Path(__file__).parents[2] / "scripts/build-agent-environment-image.sh"
    result = subprocess.run(
        ["bash", str(script), "local/agent:dev"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert [call[:3] for call in calls] == [
        ["environment", "image", "load"],
        ["environment", "image", "load-ci"],
        ["environment", "image", "declare"],
    ]
    assert calls[0][3].endswith("archive files/local_agent_dev-arm64.tar")
    assert calls[1][3] == calls[0][3]
    assert calls[2][3:] == [
        "local/agent:dev",
        "--digest",
        f"arm64=sha256:{'a' * 64}",
        "--digest",
        f"amd64=sha256:{'a' * 64}",
    ]
