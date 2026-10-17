"""Services on the hub machine, reached from another machine at the same
address.

A service that listens on the hub machine's loopback (a code host, for one)
is reached by every machine at the same ``http://127.0.0.1:<port>``, so that
the addresses recorded in the shared workspace work everywhere. Here, on a
machine other than the hub, each such port is forwarded to the hub over the
SSH connection the hub is already reached by.

Another program may forward the same port (the service's own tunnel, or the
other GuildBotics process of this machine). Whoever binds the port first
forwards it: while something answers there, nothing is started, and the port
is looked at again on the next round, so the forward is taken over when that
program stops.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
from collections.abc import Callable

from guildbotics.hub.connection import HubEndpoint, hub_forward_command

logger = logging.getLogger(__name__)

#: How often the forwards are looked at: a stopped forward is started again,
#: and a port that came to be wanted is forwarded, within this many seconds.
ROUND_SECONDS = 10.0
_ANSWER_TIMEOUT_SECONDS = 0.5
_STOP_TIMEOUT_SECONDS = 5.0


class HubForwards:
    """Keep the wanted ports of the hub machine forwarded to this machine."""

    def __init__(
        self,
        endpoint: HubEndpoint,
        ports: Callable[[], set[int]],
        *,
        round_seconds: float = ROUND_SECONDS,
    ) -> None:
        """
        Args:
            endpoint: The hub machine.
            ports: The ports wanted now; asked every round, so a change of
                the configuration takes effect without a restart.
            round_seconds: How long to wait between rounds.
        """
        self._endpoint = endpoint
        self._ports = ports
        self._round_seconds = round_seconds
        self._running: dict[int, subprocess.Popen[bytes]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="guildbotics-hub-forwards", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = _STOP_TIMEOUT_SECONDS) -> bool:
        """Stop the rounds and every forward this started."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                return False
        self._thread = None
        for port in list(self._running):
            self._end(port)
        return True

    def round(self) -> None:
        """Start the wanted forwards nothing serves, and end the others.

        Rounds run one at a time on the keeper's thread (``start``); a test
        drives them itself instead, without starting it.
        """
        try:
            wanted = self._ports()
        # A configuration that cannot be read must not end the rounds.
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Could not read which ports to forward", exc_info=True)
            return
        for port, process in list(self._running.items()):
            if port not in wanted or process.poll() is not None:
                self._end(port)
        for port in sorted(wanted - set(self._running)):
            if _answers(port):
                continue
            try:
                self._running[port] = subprocess.Popen(
                    hub_forward_command(self._endpoint, port),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                logger.warning(
                    "Could not forward port %s to %s",
                    port,
                    self._endpoint.target,
                    exc_info=True,
                )

    def _run(self) -> None:
        while True:
            self.round()
            if self._stop.wait(self._round_seconds):
                return

    def _end(self, port: int) -> None:
        process = self._running.pop(port)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _answers(port: int) -> bool:
    """Whether something already listens on this machine's loopback ``port``."""
    try:
        with socket.create_connection(
            ("127.0.0.1", port), timeout=_ANSWER_TIMEOUT_SECONDS
        ):
            return True
    except OSError:
        return False
