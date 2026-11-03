"""Name-only indices written by Python; regenerate with python -m this module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from guildbotics.utils.secret_store import KeyringSecretStore

GOLDEN = Path(__file__).resolve().parents[2] / "records/golden/secrets.json"


def indices(root: Path, keys: list[str]) -> dict:
    """Use Python's actual index writers, with no access to an OS keychain."""
    store = KeyringSecretStore(root / ".guildbotics/config", keychain=object())
    store._write_index(sorted(keys))
    store._write_local({"store_id": "synthetic-python-store"})
    return {
        "index": store.location.read_text(encoding="utf-8"),
        "local": store._local_index.read_text(encoding="utf-8"),
        "keys": store.keys(),
        "store_id": store._read_local()["store_id"],
    }


def test_python_secret_indices_match_golden(tmp_path):
    for case in json.loads(GOLDEN.read_text(encoding="utf-8")):
        assert indices(tmp_path, case["keys"]) == case


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with patch.dict(os.environ, {"GUILDBOTICS_HOME": str(root / "machine")}):
            cases = [
                indices(root, keys)
                for keys in [
                    [],
                    ["API_KEY", "TOKEN", "1_MEMBER_TOKEN"],
                    ["on", "null", "TRUE"],
                ]
            ]
    GOLDEN.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
