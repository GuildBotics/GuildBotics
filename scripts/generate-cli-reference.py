from __future__ import annotations

import argparse
from pathlib import Path

from guildbotics.cli import main
from guildbotics.cli.reference import write_cli_reference

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "docs" / "cli_reference.md"


def main_script() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the GuildBotics CLI reference."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the committed reference differs from the generated output.",
    )
    args = parser.parse_args()
    current = write_cli_reference(
        main,
        OUTPUT_PATH,
        prog_name="guildbotics",
        check=args.check,
    )
    if not current:
        parser.error(
            "docs/cli_reference.md is out of date; run this script without --check"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main_script())
