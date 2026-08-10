"""Guards for CLI help completeness and rendering.

docs/cli_reference.md renders whatever the Click definitions carry, so the
reference regeneration test alone cannot detect deleted help texts or a lost
root ``show_default`` setting (regenerating would simply bake the regression
into the reference). These tests assert the source definitions directly.
"""

from __future__ import annotations

from collections.abc import Iterator

import click
from click.testing import CliRunner

from guildbotics import cli as cli_module
from guildbotics.cli import main
from guildbotics.utils.fileio import GUILDBOTICS_DATA_DIR


def _iter_commands(
    command: click.Command, ctx: click.Context, path: str
) -> Iterator[tuple[str, click.Command, click.Context]]:
    yield path, command, ctx
    if isinstance(command, click.Group):
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            sub_ctx = click.Context(sub, info_name=name, parent=ctx)
            yield from _iter_commands(sub, sub_ctx, f"{path} {name}")


def test_every_visible_command_and_option_has_help() -> None:
    root = click.Context(main, info_name="guildbotics")
    missing: list[str] = []
    for path, command, ctx in _iter_commands(main, root, "guildbotics"):
        if not (command.help or command.short_help):
            missing.append(f"{path}: command description")
        missing.extend(
            f"{path}: {param.opts[0]}"
            for param in command.get_params(ctx)
            if isinstance(param, click.Option) and not param.hidden and not param.help
        )
    assert missing == []


def test_help_shows_defaults_required_and_repeatable(monkeypatch, tmp_path) -> None:
    """The root show_default setting and the option help conventions must
    surface in real --help output, not only in the generated reference."""
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(tmp_path / "data"))
    runner = CliRunner()

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["member", "memory", "recall", "--help"])
    assert result.exit_code == 0
    assert "[default: 20; 1<=x<=200]" in result.output
    assert "[default: json]" in result.output
    assert "[required]" in result.output
    assert "repeat for OR matching" in result.output

    result = runner.invoke(main, ["stop", "--help"])
    assert result.exit_code == 0
    assert "[default: 30]" in result.output


def test_windows_cli_configures_standard_streams_as_utf8(monkeypatch) -> None:
    class ReconfigurableStream:
        def __init__(self) -> None:
            self.encodings: list[str] = []

        def reconfigure(self, *, encoding: str) -> None:
            self.encodings.append(encoding)

    stdout = ReconfigurableStream()
    stderr = ReconfigurableStream()
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)
    monkeypatch.setattr(cli_module.sys, "stderr", stderr)

    cli_module._configure_windows_standard_streams()

    assert stdout.encodings == ["utf-8"]
    assert stderr.encodings == ["utf-8"]


def test_non_windows_cli_preserves_standard_stream_encoding(monkeypatch) -> None:
    class ReconfigurableStream:
        def __init__(self) -> None:
            self.called = False

        def reconfigure(self, *, encoding: str) -> None:
            self.called = True

    stdout = ReconfigurableStream()
    stderr = ReconfigurableStream()
    monkeypatch.setattr(cli_module.sys, "platform", "linux")
    monkeypatch.setattr(cli_module.sys, "stdout", stdout)
    monkeypatch.setattr(cli_module.sys, "stderr", stderr)

    cli_module._configure_windows_standard_streams()

    assert not stdout.called
    assert not stderr.called
