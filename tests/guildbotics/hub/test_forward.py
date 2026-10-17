"""The hub machine's services, forwarded to another machine: the ports wanted
are forwarded while nothing else serves them, a forward that ended is started
again, and a port no longer wanted is let go."""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Iterator

import pytest

from guildbotics.hub import connection, forward
from guildbotics.hub.connection import HubEndpoint
from guildbotics.hub.forward import HubForwards

HUB = HubEndpoint("hub", user="me")


class _Ssh:
    """Stands in for OpenSSH: each forward is a process that waits."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.forwarded: list[int] = []
        self.taken: set[int] = set()

        def command(endpoint: HubEndpoint, port: int) -> list[str]:
            assert endpoint == HUB
            self.forwarded.append(port)
            return [sys.executable, "-c", "import time; time.sleep(60)"]

        monkeypatch.setattr(forward, "hub_forward_command", command)
        monkeypatch.setattr(forward, "_answers", lambda port: port in self.taken)


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> _Ssh:
    return _Ssh(monkeypatch)


@pytest.fixture
def wanted() -> set[int]:
    return {7878}


@pytest.fixture
def forwards(wanted: set[int]) -> Iterator[HubForwards]:
    keeper = HubForwards(HUB, lambda: set(wanted))
    yield keeper
    keeper.stop()


def test_a_wanted_port_is_forwarded_once_and_kept(
    ssh: _Ssh, forwards: HubForwards
) -> None:
    forwards.round()
    forwards.round()

    assert ssh.forwarded == [7878]


def test_a_port_something_else_serves_is_left_to_it_and_taken_over_after(
    ssh: _Ssh, forwards: HubForwards
) -> None:
    ssh.taken.add(7878)
    forwards.round()
    assert ssh.forwarded == []

    ssh.taken.clear()
    forwards.round()
    assert ssh.forwarded == [7878]


def test_a_forward_that_ended_is_started_again(
    ssh: _Ssh, forwards: HubForwards
) -> None:
    forwards.round()
    forwards._running[7878].kill()
    forwards._running[7878].wait()

    forwards.round()

    assert ssh.forwarded == [7878, 7878]
    assert forwards._running[7878].poll() is None


def test_a_port_no_longer_wanted_is_let_go_and_a_new_one_forwarded(
    ssh: _Ssh, forwards: HubForwards, wanted: set[int]
) -> None:
    forwards.round()
    first = forwards._running[7878]

    wanted.clear()
    wanted.add(17900)
    forwards.round()

    assert first.poll() is not None
    assert set(forwards._running) == {17900}
    assert ssh.forwarded == [7878, 17900]


def test_a_configuration_that_cannot_be_read_keeps_the_forwards(
    ssh: _Ssh,
) -> None:
    calls = iter([{7878}])

    def ports() -> set[int]:
        try:
            return next(calls)
        except StopIteration:
            raise ValueError("unreadable project.yml") from None

    keeper = HubForwards(HUB, ports)
    try:
        keeper.round()
        keeper.round()
        assert keeper._running[7878].poll() is None
    finally:
        keeper.stop()


def test_stopping_ends_every_forward(ssh: _Ssh, wanted: set[int]) -> None:
    wanted.add(17900)
    keeper = HubForwards(HUB, lambda: set(wanted), round_seconds=60)
    keeper.start()
    # The first round runs at once on the keeper's own thread.
    deadline = time.monotonic() + 10
    while len(keeper._running) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    started = list(keeper._running.values())

    assert keeper.stop() is True

    assert len(started) == 2
    assert all(process.poll() is not None for process in started)
    assert keeper._running == {}


def test_a_port_answers_while_something_listens_on_it() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert forward._answers(port) is True
    assert forward._answers(port) is False


def test_the_forward_binds_the_same_loopback_port_on_both_machines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(connection, "ssh_executable", lambda: "ssh")

    command = connection.hub_forward_command(HUB, 7878)

    assert command[0] == "ssh"
    assert command[-1] == "me@hub"
    assert command[command.index("-L") + 1] == "127.0.0.1:7878:127.0.0.1:7878"
    assert "-N" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "BatchMode=yes" in command
