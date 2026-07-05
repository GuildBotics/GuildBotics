from __future__ import annotations

import signal
from pathlib import Path

from click.testing import CliRunner

from guildbotics.cli import main as cli_main


def _setup_stop(monkeypatch, tmp_path: Path, *, dies_after_sigterms: int | None):
    """Wire a fake scheduler process behind the stop command.

    The process exits once it has received ``dies_after_sigterms`` SIGTERMs
    (``None`` means SIGTERM never stops it), and always exits on SIGKILL.
    """
    pid_path = tmp_path / "scheduler.pid"
    pid_path.write_text("4242")
    monkeypatch.setattr("guildbotics.cli._pid_file_path", lambda: pid_path)
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
    return pid_path, signals


def test_stop_force_cancels_in_flight_work_before_sigkill(monkeypatch, tmp_path):
    pid_path, signals = _setup_stop(monkeypatch, tmp_path, dies_after_sigterms=2)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["stop", "--force", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert signals == [signal.SIGTERM, signal.SIGTERM]
    assert "cancelling in-flight work" in result.output.lower()
    assert not pid_path.exists()


def test_stop_force_sigkills_as_last_resort(monkeypatch, tmp_path):
    pid_path, signals = _setup_stop(monkeypatch, tmp_path, dies_after_sigterms=None)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["stop", "--force", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert signals == [signal.SIGTERM, signal.SIGTERM, signal.SIGKILL]
    assert "force killed scheduler" in result.output.lower()
    assert not pid_path.exists()


def test_stop_without_force_hints_at_escalation_on_timeout(monkeypatch, tmp_path):
    pid_path, signals = _setup_stop(monkeypatch, tmp_path, dies_after_sigterms=None)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["stop", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert signals == [signal.SIGTERM]
    assert "guildbotics stop" in result.output
    assert pid_path.exists()


def test_stop_reports_stopped_when_process_exits_gracefully(monkeypatch, tmp_path):
    pid_path, signals = _setup_stop(monkeypatch, tmp_path, dies_after_sigterms=1)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["stop", "--timeout", "0"])

    assert result.exit_code == 0, result.output
    assert signals == [signal.SIGTERM]
    assert "Scheduler stopped." in result.output
    assert not pid_path.exists()
