from __future__ import annotations

import signal
from pathlib import Path

from click.testing import CliRunner

from guildbotics.cli import main as cli_main
from guildbotics.runtime.service_lock import ServiceLock
from guildbotics.utils.i18n_tool import t


def _setup_stop(monkeypatch, tmp_path: Path, *, dies_after_sigterms: int | None):
    """Wire a fake scheduler process behind the stop command.

    The process exits once it has received ``dies_after_sigterms`` SIGTERMs
    (``None`` means SIGTERM never stops it), and always exits on SIGKILL.
    """
    lock_path = tmp_path / "service.lock"
    monkeypatch.setattr("guildbotics.runtime.service_lock.os.getpid", lambda: 4242)
    service_lock = ServiceLock(lock_path)
    service_lock.acquire(owner="cli", workspace=tmp_path)
    monkeypatch.setattr("guildbotics.cli._service_lock_path", lambda: lock_path)
    monkeypatch.setattr("guildbotics.cli._load_env_from_cwd", lambda: None)
    monkeypatch.setattr("guildbotics.cli.time.sleep", lambda _s: None)

    state = {"running": True}
    signals: list[int] = []

    def _kill(pid, sig):
        assert pid == 4242
        signals.append(sig)
        if sig == signal.SIGKILL:
            state["running"] = False
        elif (
            sig == signal.SIGTERM
            and dies_after_sigterms is not None
            and signals.count(signal.SIGTERM) >= dies_after_sigterms
        ):
            state["running"] = False

    monkeypatch.setattr("guildbotics.cli.os.kill", _kill)
    monkeypatch.setattr(
        "guildbotics.cli._pid_is_running", lambda _pid: state["running"]
    )
    return service_lock, lock_path, signals


def test_stop_force_cancels_in_flight_work_before_sigkill(monkeypatch, tmp_path):
    service_lock, lock_path, signals = _setup_stop(
        monkeypatch, tmp_path, dies_after_sigterms=2
    )
    try:
        result = CliRunner().invoke(
            cli_main, ["stop", "--force", "--timeout", "0"]
        )

        assert result.exit_code == 0, result.output
        assert signals == [signal.SIGTERM, signal.SIGTERM]
        assert "cancelling in-flight work" in result.output.lower()
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_force_sigkills_as_last_resort(monkeypatch, tmp_path):
    service_lock, lock_path, signals = _setup_stop(
        monkeypatch, tmp_path, dies_after_sigterms=None
    )
    try:
        result = CliRunner().invoke(
            cli_main, ["stop", "--force", "--timeout", "0"]
        )

        assert result.exit_code == 0, result.output
        assert signals == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]
        assert t("runtime.service_lock.force_killed") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_without_force_hints_at_escalation_on_timeout(monkeypatch, tmp_path):
    service_lock, lock_path, signals = _setup_stop(
        monkeypatch, tmp_path, dies_after_sigterms=None
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert signals == [signal.SIGTERM]
        assert t("runtime.service_lock.stop_timeout") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_reports_stopped_when_process_exits_gracefully(monkeypatch, tmp_path):
    service_lock, lock_path, signals = _setup_stop(
        monkeypatch, tmp_path, dies_after_sigterms=1
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert signals == [signal.SIGTERM]
        assert t("runtime.service_lock.stopped") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_rejects_desktop_managed_service(monkeypatch, tmp_path):
    lock_path = tmp_path / "service.lock"
    service_lock = ServiceLock(lock_path)
    service_lock.acquire(owner="desktop", workspace=tmp_path)
    monkeypatch.setattr("guildbotics.cli._service_lock_path", lambda: lock_path)
    monkeypatch.setattr("guildbotics.cli._load_env_from_cwd", lambda: None)
    try:
        result = CliRunner().invoke(cli_main, ["stop"])
    finally:
        service_lock.release()

    assert result.exit_code == 1
    assert t("runtime.service_lock.desktop_managed") in result.output
