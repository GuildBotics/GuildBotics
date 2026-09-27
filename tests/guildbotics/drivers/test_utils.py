import asyncio
from types import SimpleNamespace
from typing import List

import pytest

from guildbotics.commands.metadata import CommandAccess
from guildbotics.drivers.command_runner import HostRunLedger, host_command_cwd
from guildbotics.drivers.utils import run_command


class StubLogger:
    """Minimal logger capturing info/error messages for assertions."""

    def __init__(self) -> None:
        self.infos: List[str] = []
        self.errors: List[str] = []

    def info(self, msg: str) -> None:  # pragma: no cover - trivial
        self.infos.append(str(msg))

    def error(self, msg: str) -> None:  # pragma: no cover - trivial
        self.errors.append(str(msg))


class FakeContext:
    """Lightweight Context stub with only members used by run_workflow."""

    def __init__(self, person_id: str = "p1") -> None:
        self.logger = StubLogger()
        self.person = SimpleNamespace(person_id=person_id)
        self.shared_state: dict[str, object] = {}


@pytest.mark.asyncio
async def test_run_command_success_logs_and_returns_true(monkeypatch):
    events = []
    ledgers = []
    cwds = []

    class FakeCommandRunner:
        access = CommandAccess()

        def __init__(self, context, command, args, cwd, *, ledger):
            self.context = context
            self.command_name = command
            self.args = args
            self.cwd = cwd
            ledgers.append(ledger)
            cwds.append(cwd)

        async def run(self):
            # Simulate successful command execution
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "guildbotics.drivers.command_runner.CommandRunner", FakeCommandRunner
    )
    monkeypatch.setattr(
        "guildbotics.drivers.utils.record_correlated_event",
        lambda **kwargs: events.append(kwargs),
    )

    ctx = FakeContext()
    ok = await run_command(ctx, "test", task_type="scheduled")
    assert ok is True
    # Completion-managed turns of a scheduled run report to the host's record.
    assert [type(ledger) for ledger in ledgers] == [HostRunLedger]
    # A command the host starts on its own works in the exchange directory.
    assert cwds == [host_command_cwd()]
    # Validate logs contain start and finish messages
    start_logs = [
        m for m in ctx.logger.infos if "Running scheduled command 'test'" in m
    ]
    finish_logs = [
        m for m in ctx.logger.infos if "Finished running scheduled command 'test'" in m
    ]
    assert start_logs, "Start log not found"
    assert finish_logs, "Finish log not found"
    assert [event["event_type"] for event in events] == [
        "command.started",
        "command.finished",
    ]


@pytest.mark.asyncio
async def test_run_command_exception_logs_and_reraises(monkeypatch):
    events = []

    class FakeCommandRunnerError:
        access = CommandAccess()

        def __init__(
            self,
            context,
            command,
            args,
            cwd,
            *,
            ledger,
        ):
            self.context = context
            self.command_name = command
            self.args = args
            self.cwd = cwd

        async def run(self):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "guildbotics.drivers.command_runner.CommandRunner", FakeCommandRunnerError
    )
    monkeypatch.setattr(
        "guildbotics.drivers.utils.record_correlated_event",
        lambda **kwargs: events.append(kwargs),
    )

    ctx = FakeContext()
    with pytest.raises(RuntimeError, match="boom"):
        await run_command(ctx, "Failing", task_type="scheduled")
    # Validate error summary and traceback were logged
    error_summary = [
        e for e in ctx.logger.errors if "Error running scheduled command 'Failing'" in e
    ]
    assert error_summary, "Error summary log not found"
    traceback_logs = [e for e in ctx.logger.errors if "RuntimeError: boom" in e]
    assert traceback_logs, "Traceback log not found"
    assert [event["event_type"] for event in events] == [
        "command.started",
        "command.failed",
    ]
    assert events[-1]["payload"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["authentication", "network", "rate_limited"])
async def test_command_failure_preserves_structured_authentication_cause(
    monkeypatch, category
):
    from guildbotics.commands.errors import CommandError
    from guildbotics.drivers.utils import run_with_logging
    from guildbotics.intelligences.brains.cli_agent import (
        CliAgentExecutionError,
        CliAgentExecutionResult,
    )

    events = []
    monkeypatch.setattr(
        "guildbotics.drivers.utils.record_correlated_event",
        lambda **kwargs: events.append(kwargs),
    )

    async def fail():
        cause = CliAgentExecutionError(
            cli_agent="codex",
            result=CliAgentExecutionResult(
                stdout="", stderr="error", returncode=1, error_category=category
            ),
        )
        raise CommandError("wrapped") from cause

    with pytest.raises(CommandError):
        await run_with_logging(FakeContext(), "test", "scheduled", fail)
    assert events[-1]["payload"]["code"] == (
        "cli_agent_authentication" if category == "authentication" else ""
    )


def test_an_exchange_directory_it_may_not_create_is_told_as_the_environment_tells_it(
    monkeypatch, tmp_path
):
    """A host-started command whose exchange directory cannot be made fails
    with the refusal the isolated environment gives for the same directory
    (on macOS: allow the app the Documents folder), not a raw OSError."""
    from pathlib import Path

    from guildbotics.commands.errors import CommandError
    from guildbotics.drivers import command_runner
    from guildbotics.intelligences.agent_environment.status import (
        filesystem_permission_problem,
    )

    denied = tmp_path / "Documents" / "GuildBotics"

    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(command_runner, "exchange_dir", lambda: denied)
    monkeypatch.setattr(Path, "mkdir", refuse)

    with pytest.raises(CommandError) as refused:
        host_command_cwd()

    assert str(refused.value) == filesystem_permission_problem(denied)
