"""PyYAML owns the answers; regenerate with python -m tests.guildbotics.test_yaml_golden."""

import json
from pathlib import Path

import pytest
import yaml

GOLDEN = Path(__file__).resolve().parents[2] / "records/golden/yaml.json"


@pytest.mark.parametrize("case", json.loads(GOLDEN.read_text()))
def test_yaml_values(case):
    assert yaml.safe_load(case["source"]) == case["expected"]


if __name__ == "__main__":
    cases = json.loads(GOLDEN.read_text())
    for case in cases:
        case["expected"] = yaml.safe_load(case["source"])
    GOLDEN.write_text(json.dumps(cases, ensure_ascii=False, indent=1) + "\n")
