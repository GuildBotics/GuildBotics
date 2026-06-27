from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from guildbotics.cli import main as cli_main


class _FakeEdition:
    def __init__(self, context):
        self._context = context

    def get_context(self, message: str = ""):
        return self._context

    def get_default_routines(self) -> list[str]:
        return ["workflows/ticket_driven_workflow"]


class _FakeScheduler:
    def __init__(
        self,
        context,
        routine_commands,
        consecutive_error_limit,
        routine_interval_minutes=10,
        scheduled_source_enabled=True,
        routine_source_enabled=True,
        event_queue_source_enabled=True,
    ):
        self.context = context
        self.routine_commands = list(routine_commands)
        self.consecutive_error_limit = consecutive_error_limit
        self.routine_interval_minutes = routine_interval_minutes
        self.scheduled_source_enabled = scheduled_source_enabled
        self.routine_source_enabled = routine_source_enabled
        self.event_queue_source_enabled = event_queue_source_enabled
        self.start_called = 0
        self.shutdown_called = 0

    def start(self):
        self.start_called += 1

    def shutdown(self, graceful: bool = True):
        self.shutdown_called += 1


class _FakeEventListenerRunner:
    def __init__(self, context):
        self.context = context
        self.start_called = 0
        self.stop_called = 0
        self.join_called = 0

    def start(self):
        self.start_called += 1

    def stop(self):
        self.stop_called += 1

    def join(self, timeout=None):
        self.join_called += 1

    def is_alive(self):
        return False


def test_pid_file_path_uses_machine_state_root(monkeypatch, tmp_path):
    from guildbotics.cli import _pid_file_path
    from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "workspace-data"))

    assert _pid_file_path() == home / ".guildbotics/data/run/scheduler.pid"


def _patch_start_dependencies(monkeypatch, tmp_path: Path):
    context = object()
    edition = _FakeEdition(context)
    created: dict[str, object] = {}
    handlers: dict[object, object] = {}
    call_order: list[str] = []

    def _scheduler_factory(*args, **kwargs):
        inst = _FakeScheduler(*args, **kwargs)
        created["scheduler"] = inst
        return inst

    def _events_factory(*args, **kwargs):
        inst = _FakeEventListenerRunner(*args, **kwargs)
        created["events"] = inst
        return inst

    monkeypatch.setattr("guildbotics.cli.get_edition", lambda: edition)
    monkeypatch.setattr("guildbotics.cli.TaskScheduler", _scheduler_factory)
    monkeypatch.setattr("guildbotics.cli.EventListenerRunner", _events_factory)
    monkeypatch.setattr("guildbotics.cli._load_env_from_cwd", lambda: None)
    monkeypatch.setattr(
        "guildbotics.cli._pid_file_path", lambda: tmp_path / "scheduler.pid"
    )
    monkeypatch.setattr(
        "guildbotics.cli.signal.signal",
        lambda sig, handler: handlers.__setitem__(sig, handler),
    )

    original_scheduler_factory = _scheduler_factory
    original_events_factory = _events_factory

    def _scheduler_factory_with_order(*args, **kwargs):
        inst = original_scheduler_factory(*args, **kwargs)
        original_start = inst.start
        original_shutdown = inst.shutdown

        def _start():
            call_order.append("scheduler.start")
            return original_start()

        def _shutdown(*, graceful=True):
            call_order.append("scheduler.shutdown")
            return original_shutdown(graceful=graceful)

        inst.start = _start
        inst.shutdown = _shutdown
        return inst

    def _events_factory_with_order(*args, **kwargs):
        inst = original_events_factory(*args, **kwargs)
        original_start = inst.start
        original_stop = inst.stop
        original_join = inst.join

        def _start():
            call_order.append("events.start")
            return original_start()

        def _stop():
            call_order.append("events.stop")
            return original_stop()

        def _join(timeout=None):
            call_order.append("events.join")
            return original_join(timeout=timeout)

        inst.start = _start
        inst.stop = _stop
        inst.join = _join
        return inst

    monkeypatch.setattr("guildbotics.cli.TaskScheduler", _scheduler_factory_with_order)
    monkeypatch.setattr(
        "guildbotics.cli.EventListenerRunner", _events_factory_with_order
    )
    return created, handlers, call_order


def test_start_only_scheduler(monkeypatch, tmp_path):
    created, _handlers, _order = _patch_start_dependencies(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["start", "--only", "scheduler"])

    assert result.exit_code == 0, result.output
    assert "scheduler" in created
    assert "events" not in created
    assert created["scheduler"].start_called == 1
    assert created["scheduler"].scheduled_source_enabled is True
    assert created["scheduler"].routine_source_enabled is True
    assert created["scheduler"].event_queue_source_enabled is False


def test_start_only_events(monkeypatch, tmp_path):
    created, _handlers, _order = _patch_start_dependencies(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["start", "--only", "events"])

    assert result.exit_code == 0, result.output
    assert "events" in created
    assert "scheduler" in created
    assert created["scheduler"].routine_commands == []
    assert created["scheduler"].scheduled_source_enabled is False
    assert created["scheduler"].routine_source_enabled is False
    assert created["scheduler"].event_queue_source_enabled is True
    assert created["scheduler"].start_called == 1
    assert created["events"].start_called == 1
    assert created["events"].stop_called >= 1
    assert created["events"].join_called >= 1


def test_start_only_events_waits_for_listener_when_scheduler_has_no_workers(
    monkeypatch, tmp_path
):
    created, handlers, call_order = _patch_start_dependencies(monkeypatch, tmp_path)
    runner = CliRunner()

    class _AliveEventListenerRunner(_FakeEventListenerRunner):
        def __init__(self, context):
            super().__init__(context)
            self.alive = False

        def start(self):
            call_order.append("events.start")
            self.start_called += 1
            self.alive = True

        def stop(self):
            call_order.append("events.stop")
            self.stop_called += 1
            self.alive = False

        def join(self, timeout=None):
            call_order.append("events.join")
            self.join_called += 1

        def is_alive(self):
            return self.alive

    def _events_factory(context):
        inst = _AliveEventListenerRunner(context)
        created["events"] = inst
        return inst

    def _sleep(_seconds):
        handlers[__import__("signal").SIGTERM](__import__("signal").SIGTERM, None)

    monkeypatch.setattr("guildbotics.cli.EventListenerRunner", _events_factory)
    monkeypatch.setattr("guildbotics.cli.time.sleep", _sleep)

    result = runner.invoke(cli_main, ["start", "--only", "events"])

    assert result.exit_code == 0, result.output
    assert created["scheduler"].start_called == 1
    assert created["events"].start_called == 1
    assert created["events"].stop_called >= 1
    assert "events.start" in call_order
    assert "events.stop" in call_order


def test_start_defaults_to_scheduler_and_events(monkeypatch, tmp_path):
    created, _handlers, _order = _patch_start_dependencies(monkeypatch, tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli_main, ["start"])

    assert result.exit_code == 0, result.output
    assert "scheduler" in created
    assert "events" in created
    assert created["scheduler"].start_called == 1
    assert created["events"].start_called == 1


def test_start_signal_handler_stops_events_then_scheduler(monkeypatch, tmp_path):
    created, handlers, call_order = _patch_start_dependencies(monkeypatch, tmp_path)
    runner = CliRunner()

    def _scheduler_start_and_signal():
        created["scheduler"].start_called += 1
        # Simulate SIGTERM while start command is running.
        handlers[__import__("signal").SIGTERM](__import__("signal").SIGTERM, None)

    # Patch after factory created by command invocation.
    # We intercept via side effect on created instance inside custom factory wrapper.
    original_factory = None

    # Replace TaskScheduler factory again to inject the special start behavior.
    def _task_scheduler_factory(
        context,
        routine_commands,
        consecutive_error_limit,
        routine_interval_minutes=10,
        scheduled_source_enabled=True,
        routine_source_enabled=True,
        event_queue_source_enabled=True,
    ):
        nonlocal original_factory
        inst = _FakeScheduler(
            context,
            routine_commands,
            consecutive_error_limit,
            routine_interval_minutes,
            scheduled_source_enabled,
            routine_source_enabled,
            event_queue_source_enabled,
        )
        original_factory = inst
        created["scheduler"] = inst

        # Preserve ordering logs
        def _start():
            call_order.append("scheduler.start")
            return _scheduler_start_and_signal()

        def _shutdown(*, graceful=True):
            call_order.append("scheduler.shutdown")
            return _FakeScheduler.shutdown(inst, graceful=graceful)

        inst.start = _start
        inst.shutdown = _shutdown
        return inst

    monkeypatch.setattr("guildbotics.cli.TaskScheduler", _task_scheduler_factory)

    result = runner.invoke(cli_main, ["start"])

    assert result.exit_code == 0, result.output
    assert created["events"].start_called == 1
    # Signal handler should stop events before scheduler shutdown.
    first_stop = call_order.index("events.stop")
    first_shutdown = call_order.index("scheduler.shutdown")
    assert first_stop < first_shutdown
