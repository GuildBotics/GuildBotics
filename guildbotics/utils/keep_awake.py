"""Keeping this machine out of idle sleep while GuildBotics works.

What holds sleep off is the operating system's own mechanism, through wakepy:
``caffeinate`` on macOS, ``SetThreadExecutionState`` on Windows, and the
desktop session's D-Bus inhibitors on Linux. Only idle sleep is held off;
closing the lid and putting the machine to sleep by hand still work. A machine
that cannot be held awake (a Linux without a desktop session) runs everything
all the same: being awake is not a precondition of any work, so a failure is
one warning for the process and nothing more.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from wakepy import ActivationResult, keep

from guildbotics.utils.log_utils import get_logger

#: Taken by the first failure and never released, so the process warns once.
_warning = threading.Lock()


class KeepAwake:
    """One hold on idle sleep, from :meth:`start` until :meth:`stop`.

    A wakepy mode must be left in the thread and context it was entered in,
    while a hold is started and stopped wherever the work it covers begins and
    ends: the service's is switched from an API request and stopped from
    another thread. So the mode lives in a thread of its own. Holds are
    independent of each other; the machine stays awake while any is held.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._held: tuple[threading.Thread, threading.Event] | None = None

    def start(self) -> None:
        """Hold idle sleep off from when this returns, unless this hold
        already does."""
        with self._guard:
            if self._held is not None:
                return
            entered = threading.Event()
            release = threading.Event()
            thread = threading.Thread(
                target=_hold,
                args=(entered, release),
                name="guildbotics-keep-awake",
                daemon=True,
            )
            thread.start()
            entered.wait()
            self._held = (thread, release)

    def stop(self) -> None:
        """Let the machine sleep again as far as this hold is concerned."""
        with self._guard:
            held, self._held = self._held, None
        if held is None:
            return
        thread, release = held
        release.set()
        thread.join()


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold idle sleep off for the span of the block, however it ends."""
    hold = KeepAwake()
    hold.start()
    try:
        yield
    finally:
        hold.stop()


def _hold(entered: threading.Event, release: threading.Event) -> None:
    try:
        with keep.running(on_fail=_warn_once):
            entered.set()
            release.wait()
    finally:
        # Whatever wakepy raised, the work it was to cover goes on.
        entered.set()


def _warn_once(result: ActivationResult) -> None:
    if _warning.acquire(blocking=False):
        get_logger().warning(
            "This machine cannot be kept awake; it may sleep while GuildBotics "
            "works. %s",
            result.get_failure_text(style="inline"),
        )
