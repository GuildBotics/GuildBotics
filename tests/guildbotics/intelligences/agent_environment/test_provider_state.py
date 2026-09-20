"""A provider's store on this device: what a turn binds, and how login fills it."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from guildbotics.intelligences.agent_environment import provider_state
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

    assert bind_state(codex, home).mounts == (
        EnvironmentMount(
            f"{guest_path(home)}/.codex/sessions", store / "sessions", False
        ),
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )
    assert (store / "sessions").is_dir()

    (store / "auth.json").write_text("{}")
    mounts = bind_state(codex, home).mounts

    assert mounts == (
        EnvironmentMount(
            f"{guest_path(home)}/.codex/auth.json", store / "auth.json", False
        ),
        EnvironmentMount(
            f"{guest_path(home)}/.codex/sessions", store / "sessions", False
        ),
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )


def test_credentials_the_tool_points_elsewhere_are_bound_as_their_directory(
    machine: Path, tmp_path: Path
) -> None:
    """A tool that renames its credentials into place cannot do so over a
    file bind, so its credentials directory is bound instead, and the tool
    is pointed at the file inside it -- in a turn and at login alike."""
    grok = cli_agent_info("grok")
    home = tmp_path / "home"
    store = provider_state_dir(grok)
    guest = f"{guest_path(home)}/.grok"

    (store / "auth").mkdir(parents=True)
    (store / "auth/auth.json").write_text("{}")

    assert has_credentials(grok)
    assert (
        EnvironmentMount(f"{guest}/auth", store / "auth", False)
        in bind_state(grok, home).mounts
    )
    assert not any(
        mount.guest == f"{guest}/auth/auth.json"
        for mount in bind_state(grok, home).mounts
    )
    assert grok.provision.environment(guest.removesuffix("/.grok")) == {
        "GROK_HOME": guest,
        "GROK_AUTH_PATH": f"{guest}/auth/auth.json",
    }
    login_guest = guest_path(home.resolve())
    assert login_spec(grok, DECLARATION, home).env == {
        "GROK_HOME": f"{login_guest}/.grok",
        "GROK_AUTH_PATH": f"{login_guest}/.grok/auth/auth.json",
    }


def test_a_writable_root_binds_the_sessions_under_the_turn_s_own_directory(
    machine: Path, tmp_path: Path
) -> None:
    """Copilot renames `config.json` into place at its state root and can be
    pointed nowhere else, so the root is a directory of the turn's own. Its
    sessions are bound under it from the store as for every other provider,
    the credentials are copied in, and nothing else of the store -- what a
    login or an earlier boundary left beside them; Copilot reads its
    instructions, hooks, MCP servers and plugins from the same root --
    reaches the turn."""
    copilot = cli_agent_info("copilot")
    home = tmp_path / "home"
    store = provider_state_dir(copilot)
    (store / "session-state").mkdir(parents=True)
    (store / "session-state/one.json").write_text('{"turn": 1}')
    (store / "config.json").write_text('{"authTokens": "..."}')
    (store / "hooks").mkdir()
    (store / "hooks/left-behind.sh").write_text("echo taken over")
    (store / "copilot-instructions.md").write_text("left behind")

    state = bind_state(copilot, home)
    root = state.mounts[0].host

    assert has_credentials(copilot)
    assert state.mounts == (
        EnvironmentMount(f"{guest_path(home)}/.copilot", root, False),
        EnvironmentMount(
            f"{guest_path(home)}/.copilot/session-state",
            store / "session-state",
            False,
        ),
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )
    assert root is not None and root != store and store not in root.parents
    assert sorted(entry.name for entry in root.iterdir()) == [
        "config.json",
        "session-state",
    ]
    assert (root / "config.json").read_text() == '{"authTokens": "..."}'
    assert not any((root / "session-state").iterdir())  # The mount point only.
    assert copilot.provision.environment(guest_path(home)) == {
        "COPILOT_HOME": f"{guest_path(home)}/.copilot"
    }


def test_only_the_persisted_entries_of_a_turn_reach_the_store_and_the_next_turn(
    machine: Path, tmp_path: Path
) -> None:
    """A turn writes what it likes into its root -- the credentials it
    refreshed, and the instructions, hooks and MCP servers a prompt could
    have told it to write -- and its sessions into the store itself, through
    the bind. Releasing the turn copies the credentials back and loses the
    rest, so the next turn's root has no trace of it."""
    copilot = cli_agent_info("copilot")
    home = tmp_path / "home"
    store = provider_state_dir(copilot)
    store.mkdir(parents=True)
    (store / "config.json").write_text('{"authTokens": "old"}')

    turn = bind_state(copilot, home)
    root = turn.mounts[0].host
    assert root is not None
    (root / "config.json").write_text('{"authTokens": "refreshed"}')
    (store / "session-state/one.json").write_text('{"turn": 1}')
    (root / "mcp-config.json").write_text('{"servers": {}}')
    (root / "copilot-instructions.md").write_text("always do this")
    (root / "hooks").mkdir()
    (root / "hooks/after-turn.sh").write_text("echo taken over")
    (root / "logs").mkdir()
    (root / "logs/turn.log").write_text("noisy")

    turn.release()

    assert not root.exists()
    assert sorted(entry.name for entry in store.iterdir()) == [
        "config.json",
        "session-state",
    ]
    assert (store / "config.json").read_text() == '{"authTokens": "refreshed"}'
    assert (store / "session-state/one.json").read_text() == '{"turn": 1}'

    next_turn = bind_state(copilot, home)
    next_root = next_turn.mounts[0].host

    assert next_root is not None and next_root != root
    assert sorted(entry.name for entry in next_root.iterdir()) == [
        "config.json",
        "session-state",
    ]
    assert next_turn.mounts[1].host == store / "session-state"


def test_what_a_turn_reached_through_a_link_stays_out_of_the_store(
    machine: Path, tmp_path: Path, symlinks
) -> None:
    """A turn writes into its root under a prompt's direction, so what it
    names there is the prompt's to choose -- including a link, which the host
    resolves on the device rather than in the guest. The copy back reads
    nothing a name leads to outside the turn's directory, so no file of the
    device is read into the store by being pointed at."""
    copilot = cli_agent_info("copilot")
    home = tmp_path / "home"
    store = provider_state_dir(copilot)
    store.mkdir(parents=True)
    (store / "config.json").write_text('{"authTokens": "old"}')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("the device's own")

    turn = bind_state(copilot, home)
    root = turn.mounts[0].host
    assert root is not None
    (root / "config.json").unlink()
    (root / "config.json").symlink_to(elsewhere / "secret.txt")

    turn.release()

    assert (store / "config.json").read_text() == '{"authTokens": "old"}'
    assert not any(
        path.is_file() and path.read_bytes() == b"the device's own"
        for path in store.rglob("*")
    )


def test_a_link_in_the_store_reaches_no_turn_and_is_not_written_through(
    machine: Path, tmp_path: Path, symlinks
) -> None:
    """The other half of the boundary: a link left in the store -- a login
    runs with the whole store bound -- neither binds a directory of the
    device into a turn, nor carries a file of the device into the turn's
    root, nor takes what the turn wrote wherever it points."""
    copilot = cli_agent_info("copilot")
    home = tmp_path / "home"
    store = provider_state_dir(copilot)
    store.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sessions").mkdir(parents=True)
    (elsewhere / "secret.txt").write_text("the device's own")
    (store / "config.json").symlink_to(elsewhere / "secret.txt")
    (store / "session-state").symlink_to(
        elsewhere / "sessions", target_is_directory=True
    )

    turn = bind_state(copilot, home)
    root = turn.mounts[0].host
    assert root is not None

    assert not has_credentials(copilot)
    assert not (root / "config.json").exists()
    assert [mount.guest for mount in turn.mounts] == [
        f"{guest_path(home)}/.copilot",
        f"{guest_path(home)}/.cache",
    ]

    (root / "config.json").write_text('{"authTokens": "refreshed"}')
    turn.release()

    assert (elsewhere / "secret.txt").read_text() == "the device's own"
    assert (store / "config.json").is_symlink()


def test_a_bound_entry_a_link_stands_for_is_left_where_it_is(
    machine: Path, tmp_path: Path, symlinks
) -> None:
    """A provider whose entries are bound from the store binds them by name,
    and a link is a path of the device's rather than the store's: it is
    neither bound into a turn nor read as a saved login."""
    codex = cli_agent_info("codex")
    home = tmp_path / "home"
    store = provider_state_dir(codex)
    store.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "sessions").mkdir(parents=True)
    (elsewhere / "auth.json").write_text("{}")
    (store / "auth.json").symlink_to(elsewhere / "auth.json")
    (store / "sessions").symlink_to(elsewhere / "sessions", target_is_directory=True)

    mounts = bind_state(codex, home).mounts

    assert not has_credentials(codex)
    assert mounts == (
        EnvironmentMount(f"{guest_path(home)}/.cache", cache_dir(), False),
    )


def test_releasing_a_bound_store_leaves_the_store_alone(
    machine: Path, tmp_path: Path
) -> None:
    """A provider whose entries are bound from the store wrote into it as the
    turn went, so there is nothing to take back."""
    codex = cli_agent_info("codex")
    home = tmp_path / "home"
    state = bind_state(codex, home)
    (provider_state_dir(codex) / "sessions/one.json").write_text('{"turn": 1}')

    state.release()

    assert state.turn_dir is None
    assert (provider_state_dir(codex) / "sessions/one.json").is_file()


def test_what_a_killed_run_left_behind_is_removed_by_the_next_turn(
    machine: Path, tmp_path: Path
) -> None:
    """A run that was killed never released its turn directory; the next turn
    of that provider removes what is too old to belong to a live one."""
    copilot = cli_agent_info("copilot")
    home = tmp_path / "home"
    turns = machine / "data/agent_environment/copilot/turns"
    turns.mkdir(parents=True)
    killed, running = turns / "killed", turns / "running"
    for left in (killed, running):
        left.mkdir()
        (left / "config.json").write_text("{}")
    os.utime(killed, (0, 0))

    root = bind_state(copilot, home).mounts[0].host

    assert not killed.exists()
    assert running.is_dir()
    assert root is not None and root.parent == turns


def test_the_login_environment_mounts_the_whole_store_and_opens_egress(
    machine: Path, tmp_path: Path
) -> None:
    claude = cli_agent_info("claude")
    home = tmp_path / "home"

    spec = login_spec(claude, DECLARATION, home)

    guest = guest_path(home.resolve())
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
    assert (_Environment.started["memory_mib"], _Environment.started["cpus"]) == (
        DECLARATION.resources.memory_mib,
        DECLARATION.resources.cpus,
    )
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
    mounts = bind_state(codex, machine / "home").mounts
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


@pytest.mark.parametrize("name", ["codex", "claude", "grok", "copilot", "antigravity"])
def test_input_only_turn_has_credentials_without_sessions_or_cache(machine, name):
    tool = cli_agent_info(name)
    store = provider_state_dir(tool)
    auth = store / tool.provision.auth
    auth.parent.mkdir(parents=True, exist_ok=True)
    auth.write_text("credential")
    for entry in tool.provision.persisted:
        if entry.endswith("/"):
            (store / entry).mkdir(parents=True, exist_ok=True)
            (store / entry / "prior-conversation").write_text("old input")
    state = bind_state(tool, input_only=True)
    assert state.turn_dir is not None
    assert len(state.mounts) == 1
    assert state.mounts[0].host == state.turn_dir
    assert (state.turn_dir / tool.provision.auth).read_text() == "credential"
    assert not list(state.turn_dir.rglob("prior-conversation"))
    (state.turn_dir / tool.provision.auth).write_text("refreshed")
    state.release()
    assert auth.read_text() == "refreshed"
    assert not state.turn_dir.exists()
