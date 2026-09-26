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
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

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
from guildbotics.intelligences.agent_runtime.environment import CODE_MOUNT, CODE_ROOT
from guildbotics.utils.fileio import GUILDBOTICS_WORKSPACE_ROOT

#: The home the snapshot was built with; the suite's own fixtures move HOME.
_REAL_HOME = Path.home()

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GUILDBOTICS_CONTRACT_PROBE") != "1",
        reason="Set GUILDBOTICS_CONTRACT_PROBE=1 to probe the agent environment.",
    ),
    pytest.mark.asyncio,
]

#: How GuildBotics' code is run inside: the snapshot's Python, pointed at the
#: mount, and nothing written beside the code (the mount is read-only).
_PYTHON = f"PYTHONPATH={CODE_ROOT} PYTHONDONTWRITEBYTECODE=1 {VENV}/bin/python"

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
    params={}, args=[], stdin_override=None, cwd=work, command_index=0, config={},
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
