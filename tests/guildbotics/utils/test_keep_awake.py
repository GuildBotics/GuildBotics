"""A hold on idle sleep: held while it is started, from whichever thread.

wakepy's own fake (``WAKEPY_FAKE_SUCCESS``, set for every test) succeeds
without the operating system, and its forced failure
(``WAKEPY_FORCE_FAILURE``) fails before any is asked. What is held is read
from wakepy's count of the modes entered in this process.
"""

from __future__ import annotations

import logging
import threading
import warnings

import pytest
import wakepy

from guildbotics.utils import keep_awake as keep_awake_module
from guildbotics.utils.keep_awake import KeepAwake, keep_awake


def test_a_hold_is_held_from_start_until_stop() -> None:
    hold = KeepAwake()

    hold.start()
    held = wakepy.modecount()
    hold.stop()

    assert held == 1
    assert wakepy.modecount() == 0


def test_starting_and_stopping_twice_is_one_hold() -> None:
    hold = KeepAwake()

    hold.start()
    hold.start()
    held = wakepy.modecount()
    hold.stop()
    hold.stop()

    assert held == 1
    assert wakepy.modecount() == 0


def test_another_thread_can_stop_a_hold() -> None:
    hold = KeepAwake()
    hold.start()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        stopper = threading.Thread(target=hold.stop)
        stopper.start()
        stopper.join()

    assert wakepy.modecount() == 0


def test_holds_are_independent() -> None:
    first, second = KeepAwake(), KeepAwake()

    first.start()
    second.start()
    both = wakepy.modecount()
    first.stop()
    one = wakepy.modecount()
    second.stop()

    assert (both, one) == (2, 1)
    assert wakepy.modecount() == 0


def test_the_block_is_held_and_released_however_it_ends() -> None:
    with pytest.raises(RuntimeError, match="boom"), keep_awake():
        held = wakepy.modecount()
        raise RuntimeError("boom")

    assert held == 1
    assert wakepy.modecount() == 0


def test_a_machine_that_cannot_be_held_awake_runs_on_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("WAKEPY_FAKE_SUCCESS")
    monkeypatch.setenv("WAKEPY_FORCE_FAILURE", "1")
    monkeypatch.setattr(keep_awake_module, "_warning", threading.Lock())
    ran = []

    with caplog.at_level(logging.WARNING):
        with keep_awake():
            ran.append("first")
        with keep_awake():
            ran.append("second")

    warned = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("guildbotics")
    ]
    assert ran == ["first", "second"]
    assert len(warned) == 1
    assert warned[0].startswith("This machine cannot be kept awake")
    assert wakepy.modecount() == 0
