"""``cli.main`` attaches a diagnostics log handler for the whole process.

A test that runs it must not leave that handler on the ``guildbotics`` logger.
The next test on the worker would otherwise record the handler's log lines in
whatever diagnostics store it installs.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

from click.testing import CliRunner

from guildbotics.cli import main
from guildbotics.observability.diagnostics_events import DiagnosticsLogHandler
from tests.conftest import _restore_logger_handlers

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROBE = textwrap.dedent(
    """\
    import logging

    from click.testing import CliRunner

    from guildbotics.cli import main
    from guildbotics.observability.diagnostics_events import DiagnosticsLogHandler

    RAN = False


    def test_running_the_cli_attaches_a_diagnostics_log_handler():
        global RAN
        logger = logging.getLogger("guildbotics")
        before = [
            handler
            for handler in logger.handlers
            if isinstance(handler, DiagnosticsLogHandler)
        ]
        # Root ``--help`` never calls ``main``; a subcommand does.
        result = CliRunner().invoke(main, ["version"])
        assert result.exit_code == 0, result.output
        after = [
            handler
            for handler in logger.handlers
            if isinstance(handler, DiagnosticsLogHandler)
        ]
        assert len(after) == len(before) + 1
        RAN = True


    def test_the_diagnostics_log_handler_is_gone_after_that_test():
        assert RAN, "the CLI test must run first, in this process"
        logger = logging.getLogger("guildbotics")
        assert not any(
            isinstance(handler, DiagnosticsLogHandler) for handler in logger.handlers
        )
    """
)


def test_cli_main_does_not_leave_handlers_it_attached() -> None:
    """Handlers from before the call stay; the diagnostics handler does not.

    Root ``--help`` never enters ``main``. A real subcommand does, and that
    is what leaves the handler behind.
    """
    logger = logging.getLogger("guildbotics")
    suite_before = list(logger.handlers)
    preexisting = logging.NullHandler()
    logger.addHandler(preexisting)
    try:
        before = list(logger.handlers)
        result = CliRunner().invoke(main, ["version"])
        assert result.exit_code == 0, result.output
        assert any(
            isinstance(handler, DiagnosticsLogHandler) and handler not in before
            for handler in logger.handlers
        )
        _restore_logger_handlers(logger, before)
        assert logger.handlers == before
    finally:
        _restore_logger_handlers(logger, suite_before)


def test_the_suite_drops_the_diagnostics_log_handler_after_the_test() -> None:
    """The autouse fixture restores the logger after the test, not only when
    a test calls the helper itself."""
    probe_dir = _REPO_ROOT / "tests" / f".handler-probe-{uuid.uuid4().hex}"
    probe_dir.mkdir()
    (probe_dir / "test_handler_removed_after_cli.py").write_text(
        _PROBE, encoding="utf-8"
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe_dir / "test_handler_removed_after_cli.py"),
                "-p",
                "no:xdist",
                "-p",
                "no:randomly",
                "-q",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "2 passed" in completed.stdout
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
