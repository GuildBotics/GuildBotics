"""A provider's store on this device: what a turn binds, and where login runs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import (
    credential_vault,
    provider_state,
)
from guildbotics.intelligences.agent_environment.provider_state import (
    bind_state,
    cache_dir,
    has_credentials,
    login,
    login_spec,
    provider_state_dir,
)
from guildbotics.intelligences.agent_environment.spec import (
    EnvironmentMount,
    guest_path,
)
from guildbotics.intelligences.agent_environment.toolchain import parse_toolchain
from guildbotics.intelligences.cli_agents import cli_agent_info

DECLARATION = parse_toolchain({"dns": {"nameservers": ["10.0.0.53"]}}, where="t")
CLAUDE = cli_agent_info("claude")


@pytest.fixture
def machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for module in (provider_state, credential_vault):
        monkeypatch.setattr(
            module,
            "get_machine_state_path",
            lambda *parts: tmp_path.joinpath("data", *parts),
        )
    return tmp_path


def test_the_store_mirrors_the_state_root_under_home(machine: Path) -> None:
    assert (
        provider_state_dir(CLAUDE) == machine / "data/agent_environment/claude/.claude"
    )
    assert cache_dir() == machine / "data/agent_environment/cache"


def test_a_turn_binds_only_the_persisted_entries(machine: Path, tmp_path: Path) -> None:
    """Directories are made so the first turn can write sessions; an account
    file is bound only once login has written it."""
    home = tmp_path / "home"
    store = provider_state_dir(CLAUDE)
    root = f"{guest_path(home)}/.claude"

    assert bind_state(CLAUDE, home) == (
        EnvironmentMount(f"{root}/projects", store / "projects", False),
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )
    assert (store / "projects").is_dir()

    (store / ".claude.json").write_text("{}")

    assert bind_state(CLAUDE, home) == (
        EnvironmentMount(f"{root}/.claude.json", store / ".claude.json", False),
        EnvironmentMount(f"{root}/projects", store / "projects", False),
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )


def test_a_login_left_in_the_store_is_neither_counted_nor_bound(
    machine: Path, tmp_path: Path
) -> None:
    """A plain login an earlier GuildBotics kept, where the tool would read
    it, stays out of every turn and of the state."""
    grok = cli_agent_info("grok")
    home = tmp_path / "home"
    store = provider_state_dir(grok)
    (store / "auth").mkdir(parents=True)
    (store / "auth/auth.json").write_text("{}")

    assert not has_credentials(grok)
    assert all("auth" not in mount.guest for mount in bind_state(grok, home))
    login_guest = guest_path(home.resolve())
    spec = login_spec(grok, DECLARATION, home)
    assert spec.mounts == (EnvironmentMount(f"{login_guest}/.grok", None, False),)
    assert spec.env == {
        "GROK_HOME": f"{login_guest}/.grok",
        "GROK_AUTH_PATH": f"{login_guest}/.grok/auth/auth.json",
    }


def test_a_bound_entry_a_link_stands_for_is_left_where_it_is(
    machine: Path, tmp_path: Path, symlinks
) -> None:
    """Entries are bound from the store by name, and a link is a path of the
    device's rather than the store's: it is never bound into a turn."""
    home = tmp_path / "home"
    store = provider_state_dir(CLAUDE)
    store.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "projects").mkdir(parents=True)
    (elsewhere / "secret.json").write_text("the device's own")
    (store / ".claude.json").symlink_to(elsewhere / "secret.json")
    (store / "projects").symlink_to(elsewhere / "projects", target_is_directory=True)

    assert bind_state(CLAUDE, home) == (
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )


def test_the_login_runs_with_its_state_root_in_memory_and_egress_open(
    machine: Path, tmp_path: Path
) -> None:
    home = tmp_path / "home"

    spec = login_spec(CLAUDE, DECLARATION, home)

    guest = guest_path(home.resolve())
    assert spec.cwd == spec.home == guest
    assert spec.mounts == (EnvironmentMount(f"{guest}/.claude", None, False),)
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

    spec = login_spec(CLAUDE, declaration, tmp_path / "home")

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


#: What Claude Code's login leaves, synthetic.
_LOGIN = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "REAL-459",
            "refreshToken": "REFRESH-459",
            "expiresAt": 4102444800000,
        }
    }
).encode()


class _Environment:
    started: dict[str, Any] = {}
    process: _Process
    closed = False
    spec: Any

    @classmethod
    async def start(
        cls, spec: Any, *, snapshot: str, memory_mib: int, cpus: int
    ) -> _Environment:
        cls.started = {
            "spec": spec,
            "snapshot": snapshot,
            "memory_mib": memory_mib,
            "cpus": cpus,
        }
        cls.process = _Process()
        environment = cls()
        environment.spec = spec
        return environment

    async def run(self, *command: str, limit: int, tty: bool = False) -> _Process:
        _Environment.started["command"] = command
        _Environment.started["tty"] = tty
        self.process.stdout.feed_data(
            b"Open https://auth.example/device and enter ABCD-1234\n"
        )
        return self.process

    async def read_file(self, path: str) -> bytes | None:
        return _LOGIN if path.endswith("/.credentials.json") else None

    async def close(self) -> None:
        _Environment.closed = True


def test_login_runs_the_tool_inside_the_environment_and_relays_its_dialogue(
    machine: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_state, "AgentEnvironment", _Environment)
    typed = iter(["ABCD-1234\n"])
    shown: list[str] = []

    code = asyncio.run(
        login(
            CLAUDE,
            DECLARATION,
            snapshot=tmp_path / "snap",
            read_line=lambda: next(typed, None),
            write=shown.append,
            home=tmp_path / "home",
        )
    )

    assert code == 0
    assert _Environment.started["snapshot"] == str(tmp_path / "snap")
    assert (_Environment.started["memory_mib"], _Environment.started["cpus"]) == (
        DECLARATION.resources.memory_mib,
        DECLARATION.resources.cpus,
    )
    assert _Environment.started["command"] == CLAUDE.provision.login
    assert _Environment.started["spec"].network.unrestricted
    # A terminal, so a tool that asks before storing credentials can ask.
    assert _Environment.started["tty"] is True
    assert "".join(shown) == (
        "Open https://auth.example/device and enter ABCD-1234\nLogged in\n"
    )
    assert _Environment.process.written == [b"ABCD-1234\n"]
    assert _Environment.closed
    assert has_credentials(CLAUDE)


@pytest.mark.parametrize("exit_code,cleared", [(0, True), (1, False)])
def test_only_a_completed_login_clears_a_failure(
    machine, monkeypatch, exit_code, cleared
):
    provider_state.record_authentication_outcome(CLAUDE, failed=True)

    async def wait(self):
        return exit_code

    monkeypatch.setattr(provider_state, "AgentEnvironment", _Environment)
    monkeypatch.setattr(_Process, "wait", wait)
    typed = iter(["code\n"])
    assert (
        asyncio.run(
            login(
                CLAUDE,
                DECLARATION,
                snapshot=machine / "snap",
                read_line=lambda: next(typed, None),
                write=lambda _: None,
            )
        )
        == exit_code
    )
    assert provider_state.authentication_failed(CLAUDE) is not cleared
    assert has_credentials(CLAUDE) is cleared


def test_authentication_outcome_is_device_and_tool_state_outside_mounts(
    machine, monkeypatch
):
    grok = cli_agent_info("grok")
    provider_state.record_authentication_outcome(CLAUDE, failed=True)
    assert provider_state.authentication_failed(CLAUDE)
    assert not provider_state.authentication_failed(grok)
    mounts = bind_state(CLAUDE, machine / "home")
    assert all(not str(m.host).endswith("authentication-failed") for m in mounts)
    with monkeypatch.context() as other_device:
        other_device.setattr(
            provider_state,
            "get_machine_state_path",
            lambda *parts: machine.joinpath("other-device", *parts),
        )
        assert not provider_state.authentication_failed(CLAUDE)
        provider_state.record_authentication_outcome(CLAUDE, failed=False)
    assert provider_state.authentication_failed(CLAUDE)
    provider_state.record_authentication_outcome(CLAUDE, failed=False)
    assert not provider_state.authentication_failed(CLAUDE)
