from pathlib import Path

import click

from guildbotics.cli import main
from guildbotics.cli.reference import generate_cli_reference, write_cli_reference


def test_generated_cli_reference_contains_nested_commands_and_parameters() -> None:
    reference = generate_cli_reference(main, prog_name="guildbotics")

    assert "| `guildbotics member github pr create` |" in reference
    assert "`--max-consecutive-errors`" in reference
    assert "integer" in reference
    assert "Stop a worker after this many consecutive workflow errors." in reference


def test_generated_cli_reference_escapes_markdown_table_cells() -> None:
    @click.command()
    @click.option("--mode", type=click.Choice(["read", "write"]), help="Read | write.")
    def sample(mode: str | None) -> None:
        """Sample command."""

    reference = generate_cli_reference(sample, prog_name="sample")

    assert "read \\| write" in reference
    assert "Read \\| write." in reference


def test_write_cli_reference_check_detects_drift(tmp_path: Path) -> None:
    output = tmp_path / "cli_reference.md"

    assert not write_cli_reference(main, output, prog_name="guildbotics", check=True)
    assert write_cli_reference(main, output, prog_name="guildbotics")
    assert write_cli_reference(main, output, prog_name="guildbotics", check=True)

    output.write_text("outdated")
    assert not write_cli_reference(main, output, prog_name="guildbotics", check=True)
