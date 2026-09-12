"""A provider's store on this device: what a turn binds, and how login fills it."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import provider_state
from guildbotics.intelligences.agent_environment.provider_state import (
    cache_dir,
    has_credentials,
    login,
    login_spec,
    provider_state_dir,
    state_mounts,
)
from guildbotics.intelligences.agent_environment.spec import EnvironmentMount
from guildbotics.intelligences.agent_environment.toolchain import parse_toolchain
from guildbotics.intelligences.cli_agents import cli_agent_info

DECLARATION = parse_toolchain({"dns": {"nameservers": ["10.0.0.53"]}}, where="t")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        provider_state,
        "get_machine_state_path",
        lambda *parts: tmp_path.joinpath("data", *parts),
    )
    return tmp_path


def test_the_store_mirrors_the_state_root_under_home(machine: Path) -> None:
    codex = cli_agent_info("codex")

    assert provider_state_dir(codex) == machine / "data/agent_environment/codex/.codex"
    assert cache_dir() == machine / "data/agent_environment/cache"


def test_credentials_saved_means_the_credentials_file_exists(machine: Path) -> None:
    codex = cli_agent_info("codex")
    assert not has_credentials(codex)

    store = provider_state_dir(codex)
    store.mkdir(parents=True)
    (store / "auth.json").write_text("{}")

    assert has_credentials(codex)
    assert not has_credentials(cli_agent_info("grok"))


def test_a_turn_binds_only_the_persisted_entries(machine: Path, tmp_path: Path) -> None:
    """Directories are made so the first turn can write sessions; a
    credentials file is bound only once login has written it."""
    codex = cli_agent_info("codex")
    home = tmp_path / "home"
    store = provider_state_dir(codex)

    assert state_mounts(codex, home) == (
        EnvironmentMount(
            f"{home.as_posix()}/.codex/sessions", store / "sessions", False
        ),
        EnvironmentMount(f"{home.as_posix()}/.cache", cache_dir(), False),
    )
    assert (store / "sessions").is_dir()

    (store / "auth.json").write_text("{}")
    mounts = state_mounts(codex, home)

    assert mounts == (
        EnvironmentMount(
            f"{home.as_posix()}/.codex/auth.json", store / "auth.json", False
        ),
        EnvironmentMount(
            f"{home.as_posix()}/.codex/sessions", store / "sessions", False
        ),
        EnvironmentMount(f"{home.as_posix()}/.cache", cache_dir(), False),
    )


def test_the_login_environment_mounts_the_whole_store_and_opens_egress(
    machine: Path, tmp_path: Path
) -> None:
    claude = cli_agent_info("claude")
    home = tmp_path / "home"

    spec = login_spec(claude, DECLARATION, home)

    guest = home.resolve().as_posix()
    assert spec.cwd == spec.home == guest
    assert spec.mounts == (
        EnvironmentMount(f"{guest}/.claude", provider_state_dir(claude), False),
    )
    assert provider_state_dir(claude).is_dir()
    assert spec.network.unrestricted
    assert spec.network.nameservers == ("10.0.0.53",)
    assert spec.env == {"CLAUDE_CONFIG_DIR": f"{guest}/.claude"}


def test_the_login_environment_forwards_to_the_devices_resolvers_for_host(
    machine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        provider_state, "upstream_nameservers", lambda dns: ("192.168.3.1",)
    )
    declaration = parse_toolchain({"dns": {"nameservers": "host"}}, where="t")

    spec = login_spec(cli_agent_info("codex"), declaration, tmp_path / "home")

    assert spec.network.nameservers == ("192.168.3.1",)


class _Process:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = self
        self.written: list[bytes] = []
        self.closed = False
        self.returncode: int | None = None

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        # The code arrives; the login finishes.
        self.stdout.feed_data(b"Logged in\n")
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.returncode = 0

    def close(self) -> None:
        self.closed = True

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


class _Environment:
    started: dict[str, Any] = {}
    process: _Process
    closed = False

    @classmethod
    async def start(cls, spec: Any, *, snapshot: str) -> _Environment:
        cls.started = {"spec": spec, "snapshot": snapshot}
        cls.process = _Process()
        return cls()

    async def run(self, *command: str, limit: int, tty: bool = False) -> _Process:
        _Environment.started["command"] = command
        _Environment.started["tty"] = tty
        self.process.stdout.feed_data(
            b"Open https://auth.example/device and enter ABCD-1234\n"
        )
        return self.process

    async def close(self) -> None:
        _Environment.closed = True


def test_login_runs_the_tool_inside_the_environment_and_relays_its_dialogue(
    machine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_state, "AgentEnvironment", _Environment)
    codex = cli_agent_info("codex")
    typed = iter(["ABCD-1234\n"])
    shown: list[str] = []

    code = asyncio.run(
        login(
            codex,
            DECLARATION,
            snapshot=tmp_path / "snap",
            read_line=lambda: next(typed, None),
            write=shown.append,
            home=tmp_path / "home",
        )
    )

    assert code == 0
    assert _Environment.started["snapshot"] == str(tmp_path / "snap")
    assert _Environment.started["command"] == ("codex", "login", "--device-auth")
    assert _Environment.started["spec"].network.unrestricted
    # A terminal, so a tool that asks before storing credentials can ask.
    assert _Environment.started["tty"] is True
    assert "".join(shown) == (
        "Open https://auth.example/device and enter ABCD-1234\nLogged in\n"
    )
    assert _Environment.process.written == [b"ABCD-1234\n"]
    assert _Environment.closed


@pytest.mark.parametrize(
    "exit_code,stored,cleared", [(0, True, True), (0, False, False), (1, True, False)]
)
def test_only_completed_login_with_credentials_clears_failure(
    machine, monkeypatch, exit_code, stored, cleared
):
    codex = cli_agent_info("codex")
    provider_state.record_authentication_outcome(codex, failed=True)
    if stored:
        store = provider_state_dir(codex)
        store.mkdir(parents=True)
        (store / codex.provision.auth).write_text("{}")

    async def wait(self):
        return exit_code

    monkeypatch.setattr(provider_state, "AgentEnvironment", _Environment)
    monkeypatch.setattr(_Process, "wait", wait)
    typed = iter(["code\n"])
    assert (
        asyncio.run(
            login(
                codex,
                DECLARATION,
                snapshot=machine / "snap",
                read_line=lambda: next(typed, None),
                write=lambda _: None,
            )
        )
        == exit_code
    )
    assert provider_state.authentication_failed(codex) is not cleared


def test_authentication_outcome_is_device_and_tool_state_outside_mounts(
    machine, monkeypatch
):
    codex, claude = cli_agent_info("codex"), cli_agent_info("claude")
    provider_state.record_authentication_outcome(codex, failed=True)
    assert provider_state.authentication_failed(codex)
    assert not provider_state.authentication_failed(claude)
    mounts = state_mounts(codex, machine / "home")
    assert all(not str(m.host).endswith("authentication-failed") for m in mounts)
    with monkeypatch.context() as other_device:
        other_device.setattr(
            provider_state,
            "get_machine_state_path",
            lambda *parts: machine.joinpath("other-device", *parts),
        )
        assert not provider_state.authentication_failed(codex)
        provider_state.record_authentication_outcome(codex, failed=False)
    assert provider_state.authentication_failed(codex)
    provider_state.record_authentication_outcome(codex, failed=False)
    assert not provider_state.authentication_failed(codex)
