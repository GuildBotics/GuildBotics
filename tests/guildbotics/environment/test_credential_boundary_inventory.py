"""Device probe ownership must not depend on other environments staying idle."""

import asyncio
import sys
from contextlib import aclosing
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.environment import status
from tests.guildbotics.environment import (
    test_credential_boundary as probe,
)


@pytest.fixture
def inventory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the probe's own setup without inspecting or starting a real device."""
    monkeypatch.setattr(probe, "_SANDBOXES", tmp_path)
    ready = SimpleNamespace(
        refusal="",
        snapshot=SimpleNamespace(path=tmp_path / "snapshot"),
        declaration=SimpleNamespace(resources=SimpleNamespace(memory_mib=1024, cpus=1)),
        dns=SimpleNamespace(nameservers=()),
    )
    monkeypatch.setattr(probe, "device_status", lambda: ready)
    monkeypatch.setattr(status, "device_status", lambda: ready)
    ids = iter(SimpleNamespace(hex=char * 32) for char in "ab")
    monkeypatch.setattr(probe.uuid, "uuid4", lambda: next(ids))
    return tmp_path


@pytest.mark.asyncio
async def test_probe_tracks_its_vms_while_unrelated_vms_come_and_go(
    monkeypatch: pytest.MonkeyPatch, inventory: Path
) -> None:
    production = probe.runtime._NAME_PREFIX
    prefixes: set[str] = set()
    for _ in range(2):
        with monkeypatch.context() as scope:
            async with aclosing(probe.device.__wrapped__(scope, inventory)) as device:
                await anext(device)
                prefix = probe.runtime._NAME_PREFIX
                assert prefix != production and len(prefix) <= len(production)
                assert prefix not in prefixes
                prefixes.add(prefix)
                existing = inventory / f"{production}existing"
                existing.mkdir()
                before = probe._sandboxes()
                owned = inventory / f"{prefix}vm"
                owned.mkdir()
                (inventory / f"{production}concurrent-{prefix}").mkdir()
                existing.rmdir()

                assert probe._sandboxes() - before == {owned.name}
                owned.rmdir()
                assert probe._sandboxes() == before
        assert probe.runtime._NAME_PREFIX == production


def test_killed_owner_receives_the_probe_prefix_before_starting_its_vm(
    monkeypatch: pytest.MonkeyPatch, inventory: Path
) -> None:
    production = probe.runtime._NAME_PREFIX
    command: list[str] = []
    started: list[str] = []

    class Intercepted(Exception):
        """Stop before starting a real process or VM."""

    def popen(argv, **_kwargs):
        command.extend(argv)
        raise Intercepted

    async def start(*_args, **_kwargs):
        started.append(probe.runtime._NAME_PREFIX)
        raise Intercepted

    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    monkeypatch.setattr(probe.runtime.AgentEnvironment, "start", start)

    async def capture() -> str:
        async with aclosing(probe.device.__wrapped__(monkeypatch, inventory)) as device:
            ready = await anext(device)
            with pytest.raises(Intercepted):
                await probe.test_an_environment_whose_owner_is_killed_goes_with_it(
                    ready
                )
            return probe.runtime._NAME_PREFIX

    prefix = asyncio.run(capture())
    # A new interpreter starts with the production value, not the parent's patch.
    monkeypatch.setattr(probe.runtime, "_NAME_PREFIX", production)
    monkeypatch.setattr(sys, "argv", command[1:2] + command[3:])
    with pytest.raises(Intercepted):
        exec(command[2], {})
    assert started == [prefix]
