"""The Rust snapshot recipe must consume the current CLI pins."""

import runpy
from pathlib import Path


def test_environment_catalog_matches_cli_pins() -> None:
    namespace = runpy.run_path(
        str(
            Path(__file__).resolve().parents[2]
            / "scripts/generate-environment-catalog.py"
        )
    )
    assert namespace["OUTPUT"].read_text(encoding="utf-8") == namespace["render"]()
