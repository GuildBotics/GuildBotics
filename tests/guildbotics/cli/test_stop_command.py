from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from guildbotics.cli import main as cli_main
from guildbotics.runtime.service_lock import ServiceLock
from guildbotics.utils.i18n_tool import t


def _setup_stop(monkeypatch, tmp_path: Path, *, dies_after_stage: str | None):
    """Wire a fake scheduler process behind the stop command."""
    lock_path = tmp_path / "service.lock"
    monkeypatch.setattr("guildbotics.runtime.service_lock.os.getpid", lambda: 4242)
    service_lock = ServiceLock(lock_path)
    service_lock.acquire(owner="cli", workspace=tmp_path)
    monkeypatch.setattr("guildbotics.cli._service_lock_path", lambda: lock_path)
    monkeypatch.setattr(
        "guildbotics.cli._stop_request_path", lambda: tmp_path / "stop-request.json"
    )
    monkeypatch.setattr("guildbotics.cli._apply_selected_workspace", lambda: Path.cwd())
    monkeypatch.setattr("guildbotics.cli.time.sleep", lambda _s: None)

    state = {"running": True}
    requests: list[str] = []

    def _write_stop_request(instance_id, stage, path):
        assert instance_id == service_lock._metadata.service_instance_id
        assert path == tmp_path / "stop-request.json"
        requests.append(stage)
        if stage == dies_after_stage:
            state["running"] = False

    def _force_terminate(pid):
        assert pid == 4242
        requests.append("force")
        state["running"] = False

    monkeypatch.setattr("guildbotics.cli.write_stop_request", _write_stop_request)
    monkeypatch.setattr("guildbotics.cli.force_terminate_pid", _force_terminate)
    monkeypatch.setattr("guildbotics.cli.pid_exists", lambda _pid: state["running"])
    return service_lock, lock_path, requests


def test_stop_force_cancels_in_flight_work_before_force_kill(monkeypatch, tmp_path):
    service_lock, lock_path, requests = _setup_stop(
        monkeypatch, tmp_path, dies_after_stage="cancel"
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--force", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert requests == ["graceful", "cancel"]
        assert "cancelling in-flight work" in result.output.lower()
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_force_terminates_as_last_resort(monkeypatch, tmp_path):
    service_lock, lock_path, requests = _setup_stop(
        monkeypatch, tmp_path, dies_after_stage=None
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--force", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert requests == ["graceful", "cancel", "force"]
        assert t("runtime.service_lock.force_killed") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_without_force_hints_at_escalation_on_timeout(monkeypatch, tmp_path):
    service_lock, lock_path, requests = _setup_stop(
        monkeypatch, tmp_path, dies_after_stage=None
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert requests == ["graceful"]
        assert t("runtime.service_lock.stop_timeout") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


def test_stop_reports_stopped_when_process_exits_gracefully(monkeypatch, tmp_path):
    service_lock, lock_path, requests = _setup_stop(
        monkeypatch, tmp_path, dies_after_stage="graceful"
    )
    try:
        result = CliRunner().invoke(cli_main, ["stop", "--timeout", "0"])

        assert result.exit_code == 0, result.output
        assert requests == ["graceful"]
        assert t("runtime.service_lock.stopped") in result.output
        assert lock_path.exists()
    finally:
        service_lock.release()


@pytest.mark.parametrize(
    ("failed_stage", "force", "expected_requests"),
    [
        ("graceful", False, ["graceful"]),
        ("cancel", True, ["graceful", "cancel"]),
    ],
)
def test_stop_reports_control_lock_timeout_without_traceback(
    monkeypatch,
    tmp_path,
    failed_stage,
    force,
    expected_requests,
):
    service_lock, _lock_path, requests = _setup_stop(
        monkeypatch, tmp_path, dies_after_stage=None
    )

    def _write_stop_request(_instance_id, stage, _path):
        requests.append(stage)
        if stage == failed_stage:
            raise TimeoutError("lock timeout")

    monkeypatch.setattr("guildbotics.cli.write_stop_request", _write_stop_request)
    arguments = ["stop", "--timeout", "0"]
    if force:
        arguments.append("--force")
    try:
        result = CliRunner().invoke(cli_main, arguments)
    finally:
        service_lock.release()

    assert result.exit_code == 1
    assert requests == expected_requests
    assert t("runtime.service_lock.control_timeout", pid=4242) in result.output
    assert "Traceback" not in result.output


def test_stop_rejects_desktop_managed_service(monkeypatch, tmp_path):
    lock_path = tmp_path / "service.lock"
    service_lock = ServiceLock(lock_path)
    service_lock.acquire(owner="desktop", workspace=tmp_path)
    monkeypatch.setattr("guildbotics.cli._service_lock_path", lambda: lock_path)
    monkeypatch.setattr("guildbotics.cli._apply_selected_workspace", lambda: Path.cwd())
    try:
        result = CliRunner().invoke(cli_main, ["stop"])
    finally:
        service_lock.release()

    assert result.exit_code == 1
    assert t("runtime.service_lock.desktop_managed") in result.output
