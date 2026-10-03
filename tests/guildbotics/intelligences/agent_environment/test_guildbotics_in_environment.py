"""GuildBotics' own code runs inside the microVM a turn boots.

Skipped unless ``GUILDBOTICS_CONTRACT_PROBE=1``, on a device whose agent
environment is ready (point ``GUILDBOTICS_CONFIG_DIR`` at a workspace with a
built snapshot). It boots the snapshot with the code mount every turn gets,
and asserts what running GuildBotics inside relies on: the snapshot's Python
and pinned dependencies load the command execution machinery from the
read-only mount, and ``to_pdf`` draws a PDF with WeasyPrint's native
libraries and fonts that cover Japanese. It sends nothing off the device.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from zipfile import ZipFile

import pytest
import pytest_asyncio

from guildbotics.intelligences.agent_environment.contract import AccessContract
from guildbotics.intelligences.agent_environment.runtime import AgentEnvironment
from guildbotics.intelligences.agent_environment.snapshot import (
    PYTHON_VERSION,
    VENV,
)
from guildbotics.intelligences.agent_environment.spec import build_environment_spec
from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.agent_runtime.command_guest import EnvironmentGuest
from guildbotics.intelligences.agent_runtime.environment import CODE_MOUNT
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

#: The home the snapshot was built with; the suite's own fixtures move HOME.
_REAL_HOME = Path.home()

pytestmark = [
    pytest.mark.real_device("GUILDBOTICS_CONTRACT_PROBE"),
    pytest.mark.asyncio,
]

#: How GuildBotics' code is run inside: the snapshot's Python, which finds the
#: mount by itself, and nothing written beside the code (the mount is read-only).
_PYTHON = f"{VENV}/bin/python -B"

#: The inline ``to_pdf`` command, run the way the command runner runs it.
_TO_PDF = """
import asyncio, pathlib, sys
from types import SimpleNamespace

import guildbotics.drivers.command_runner  # the command execution machinery
from guildbotics.commands.models import CommandSpec
from guildbotics.commands.to_pdf_command import ToPdfCommand

work = pathlib.Path.cwd()
spec = CommandSpec(
    name="inline_to_pdf", base_dir=work, command_class=ToPdfCommand, path=None,
    params={}, args=[], stdin_override=None, cwd=work, config={},
)
context = SimpleNamespace(pipe="# 見出し\\n\\n日本語の本文 and Latin text.", shared_state={})
outcome = asyncio.run(ToPdfCommand(context, spec, work).run())
sys.stdout.write(outcome.result[:5].decode("ascii"))
"""


@pytest_asyncio.fixture
async def boot(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Callable[[Path], Awaitable[AgentEnvironment]]]:
    """Boots microVMs from this device's snapshot with the code every turn has,
    working in the directory it is given."""
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(_REAL_HOME))
    monkeypatch.delenv(GUILDBOTICS_WORKSPACE_ROOT, raising=False)
    status = device_status()
    if status.refusal or status.snapshot is None or status.declaration is None:
        pytest.skip(f"The agent environment is not ready here: {status.refusal}")
    started: list[AgentEnvironment] = []

    async def start(cwd: Path) -> AgentEnvironment:
        assert status.snapshot is not None and status.declaration is not None
        spec = build_environment_spec(
            AccessContract(),
            cwd,
            home=_REAL_HOME,
            nameservers=status.dns.nameservers,
            mounts=(CODE_MOUNT,),
        )
        environment = await AgentEnvironment.start(
            spec,
            snapshot=str(status.snapshot.path),
            memory_mib=status.declaration.resources.memory_mib,
            cpus=status.declaration.resources.cpus,
        )
        started.append(environment)
        return environment

    yield start
    for environment in started:
        await environment.close()


@pytest_asyncio.fixture
async def guest(boot, tmp_path: Path) -> AgentEnvironment:
    """A microVM working in a directory of its own."""
    return await boot(tmp_path)


async def _sh(environment: AgentEnvironment, script: str) -> tuple[int, str]:
    process = await environment.run("sh", "-c", script, limit=1 << 22)
    try:
        out, err = await asyncio.wait_for(process.communicate(), 180)
    except TimeoutError:
        await process.kill()
        raise
    text = out.decode(errors="replace") + err.decode(errors="replace")
    return await process.wait(), text


async def test_the_snapshot_python_loads_guildbotics_from_the_mount(
    guest: AgentEnvironment,
) -> None:
    code, out = await _sh(
        guest,
        f"{_PYTHON} -c 'import sys, guildbotics.drivers.command_runner, "
        "guildbotics; print(sys.version.split()[0]); print(guildbotics.__file__)'",
    )

    assert code == 0, out
    version, where = out.split()[:2]
    assert version.startswith(f"{PYTHON_VERSION}."), out
    assert where == f"{CODE_MOUNT.guest}/__init__.py"


async def test_the_code_mount_is_read_only(guest: AgentEnvironment) -> None:
    code, out = await _sh(guest, f"touch {CODE_MOUNT.guest}/probe")

    assert code != 0, out


async def test_a_turn_working_in_the_checkout_still_writes_its_package(
    boot,
) -> None:
    """Why the code is not at its host path: the checkout GuildBotics runs
    from, as a turn's working directory, keeps its ``guildbotics/`` writable
    beside the read-only copy of the same directory."""
    assert CODE_MOUNT.host is not None
    checkout = CODE_MOUNT.host.parent
    guest = await boot(checkout)
    probe = CODE_MOUNT.host / f".probe-{uuid.uuid4().hex}"
    try:
        code, out = await _sh(
            guest,
            f"touch {guest.spec.cwd}/{CODE_MOUNT.host.name}/{probe.name}",
        )
        assert code == 0, out
        assert probe.exists()
    finally:
        probe.unlink(missing_ok=True)
    code, out = await _sh(guest, f"touch {CODE_MOUNT.guest}/{probe.name}")
    assert code != 0, out


async def test_to_pdf_draws_a_pdf_with_japanese_fonts(
    guest: AgentEnvironment,
) -> None:
    code, fonts = await _sh(guest, "fc-list :lang=ja family")
    assert code == 0 and fonts.strip(), fonts

    code, out = await _sh(guest, f"{_PYTHON} - <<'PROBE'\n{_TO_PDF}\nPROBE")

    assert code == 0, out
    assert out.endswith("%PDF-"), out


async def test_an_artifact_is_unpacked_by_the_code_in_the_microvm(
    boot, tmp_path: Path, symlinks
) -> None:
    """What the host downloads for a command of the microVM, the microVM
    unpacks with GuildBotics' own Python; a link it made where it unpacks
    leads nowhere on the host, which writes nothing there itself."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "linked").symlink_to(outside, target_is_directory=True)
    archive = tmp_path / "artifact.zip"
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("report/error.md", b"details")
    environment = await boot(work)
    guest = EnvironmentGuest(asyncio.get_running_loop(), lambda: environment).until(
        time.monotonic() + 120
    )

    def unpack(destination: Path):
        return guest.run(
            guest.python(
                "guildbotics.capabilities.artifact_archive", guest.path(destination)
            ),
            cwd="/",
            env={},
            stdin=archive,
            stdout_limit=1 << 20,
        )

    unpacked = await asyncio.to_thread(unpack, work / "artifact")
    through_link = await asyncio.to_thread(unpack, work / "linked" / "artifact")

    assert unpacked.returncode == 0, unpacked.stderr
    written = work / "artifact" / "report" / "error.md"
    assert json.loads(unpacked.stdout)["files"] == [guest.path(written)]
    assert written.read_bytes() == b"details"
    assert through_link.returncode in {0, 1}, through_link.stderr
    assert list(outside.iterdir()) == []


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch) -> Path:
    """This device's workspace (``GUILDBOTICS_CONFIG_DIR``), whose snapshot
    the commands below boot from, as the host selects it."""
    monkeypatch.setenv("HOME", str(_REAL_HOME))
    monkeypatch.setenv("USERPROFILE", str(_REAL_HOME))
    config = Path(os.environ["GUILDBOTICS_CONFIG_DIR"])
    monkeypatch.setenv(GUILDBOTICS_WORKSPACE_ROOT, str(config.parent.parent))
    status = device_status()
    if status.refusal:
        pytest.skip(f"The agent environment is not ready here: {status.refusal}")
    return config


#: What a command sees of where it runs, as one line.
_WHERE = """
import logging, os, platform
from pathlib import Path

def main(context):
    logging.getLogger("guildbotics").warning("from inside")
    config = Path(os.environ["GUILDBOTICS_CONFIG_DIR"])
    try:
        (config / "probe").write_text("x")
        writes = "writes"
    except OSError:
        writes = "read-only"
    return "|".join([
        platform.system(),
        context.person.person_id,
        str(os.environ.get("GUILDBOTICS_PROBE_HOST_ONLY")),
        os.getcwd(),
        context.pipe,
        str((config / "team" / "project.yml").is_file()),
        writes,
    ])
"""


async def test_a_command_runs_in_the_microvm_the_host_boots_for_it(
    workspace, tmp_path, monkeypatch, caplog
):
    """The whole command runs in its microVM: Linux, working where it was
    asked to with its input, reading the workspace's configuration it cannot
    change, and nothing of the host's environment; what it logs is logged on
    the host."""
    import logging

    from tests.guildbotics.product_path import run_file, workspace_member

    monkeypatch.setenv("GUILDBOTICS_PROBE_HOST_ONLY", "host")
    (tmp_path / "where.py").write_text(_WHERE, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="guildbotics"):
        outcome = await run_file(tmp_path / "where.py", "the input")

    assert outcome.text_output.split("|") == [
        "Linux",
        workspace_member(),
        "None",
        str(tmp_path),
        "the input",
        "True",
        "read-only",
    ]
    assert "from inside" in caplog.text
    assert not (workspace / "probe").exists()


async def test_what_a_command_prints_or_leaves_running_does_not_hold_its_end(
    workspace, tmp_path, caplog
):
    """The command's reply is the entry's alone: what the command prints is
    logged, and a process it leaves running ends with the microVM rather
    than holding the command open until it exits."""
    import logging

    from tests.guildbotics.product_path import run_file

    (tmp_path / "leaves.py").write_text(
        "import subprocess\n"
        "def main():\n"
        "    print('printed')\n"
        "    subprocess.Popen(['sleep', '60'])\n"
        "    return 'done'\n",
        encoding="utf-8",
    )
    started = time.monotonic()

    with caplog.at_level(logging.INFO, logger="guildbotics"):
        outcome = await run_file(tmp_path / "leaves.py")

    assert outcome.text_output == "done"
    assert "printed" in caplog.text
    assert time.monotonic() - started < 30


async def test_a_subcommand_outside_what_the_command_mounted_is_refused(
    workspace, tmp_path
):
    """Nothing of the host is there, so a subcommand is not run there."""
    from guildbotics.commands.errors import CommandError
    from tests.guildbotics.product_path import run_file

    (tmp_path / "outer.yml").write_text(
        "commands:\n  - script: pwd\n    cwd: /etc\n", encoding="utf-8"
    )

    with pytest.raises(CommandError, match="/etc"):
        await run_file(tmp_path / "outer.yml")


async def test_a_template_reaches_only_what_the_microvm_holds(workspace, tmp_path):
    """A template evaluates in the command's microVM: the context it is given
    reaches the member's services through the command's window, never the
    host's objects."""
    from tests.guildbotics.product_path import run_file

    (tmp_path / "reach.md").write_text(
        "---\nbrain: none\ntemplate_engine: jinja2\n---\n"
        "{{ context.integration_factory.__class__.__module__ }}\n",
        encoding="utf-8",
    )

    outcome = await run_file(tmp_path / "reach.md")

    assert outcome.text_output.strip() == "guildbotics.integrations.window"
