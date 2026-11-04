#!/usr/bin/env python
"""Export the CLI provisioning data consumed by the Rust snapshot recipe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from guildbotics.intelligences.cli_agents import CLI_AGENTS

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "guildbotics/templates/environment/cli_provision.json"
)


def render() -> str:
    """Keep catalog order: it is part of the snapshot's build identity."""
    return (
        json.dumps(
            [
                {
                    "name": agent.name,
                    "package": agent.provision.package,
                    "install": agent.provision.install,
                    "provision": agent.provision.model_dump(mode="json"),
                }
                for agent in CLI_AGENTS
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = render()
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != expected:
            parser.exit(1, "Run scripts/generate-environment-catalog.py\n")
    else:
        OUTPUT.write_text(expected, encoding="utf-8")


if __name__ == "__main__":
    main()
