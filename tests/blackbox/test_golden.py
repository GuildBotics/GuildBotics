"""Compare a server's reads of a production workspace copy with golden responses.

Opt-in: ``GUILDBOTICS_BLACKBOX_GOLDEN`` names a directory made by
``python -m tests.blackbox.golden``, holding the copy and its ``golden/``. With
``GUILDBOTICS_BLACKBOX_GOLDEN_RECORD=1`` the reads are recorded there instead.
Each run serves a fresh copy of the copy, so no server writes to it.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from tests.blackbox.harness import block_runtime, serving

SOURCE = os.environ.get("GUILDBOTICS_BLACKBOX_GOLDEN", "")
RECORD = os.environ.get("GUILDBOTICS_BLACKBOX_GOLDEN_RECORD") == "1"
# Reads of the workspace's own files; the server's own run and this device's
# runtime are not part of them.
READS = (
    "config/status",
    "config/project",
    "config/intelligences",
    "config/providers",
    "config/roles",
    "intelligences/decisions/options",
    "intelligences/model-providers",
    "intelligences/inference-failures",
    "intelligences/cli-agents/last-turns",
    "identities",
    "team",
    "commands/options",
    "commands/routine-options",
    "commands/files",
    "service/settings",
    "transcripts/settings",
)
# Describe the server process or its machine, not the workspace's files.
VOLATILE = {
    "health": ("version", "service_instance_id", "machine_name"),
    "transcripts/settings": ("total_size_bytes", "index_size_bytes"),
}

pytestmark = [
    pytest.mark.skipif(
        not SOURCE, reason="GUILDBOTICS_BLACKBOX_GOLDEN names no production copy"
    ),
    pytest.mark.timeout(1200),
]


def _read_all(stack, end: str) -> dict:
    def get(path, **params):
        response = stack.api.get(path, params=params, timeout=120)
        assert response.status_code == 200, f"{path}: {response.text}"
        return response.json()

    reads = {path: get(path) for path in (*READS, "health")}
    for path, fields in VOLATILE.items():
        reads[path] = {k: v for k, v in reads[path].items() if k not in fields}
    reads["activity/history"] = get(
        "activity/history", start="2000-01-01T00:00:00Z", end=end
    )
    for path in ("diagnostics/traces", "diagnostics/memory-events"):
        reads[path] = get(path, limit=1000)
    for member in reads["team"]["members"]:
        person = member["person_id"]
        reads[f"config/members/{person}"] = get(f"config/members/{person}")
        reads[f"config/intelligences?person_id={person}"] = get(
            "config/intelligences", person_id=person
        )
        reads[f"commands/options?person={person}"] = get(
            "commands/options", person=person
        )
        avatar = stack.api.get(f"config/members/{person}/avatar")
        reads[f"config/members/{person}/avatar"] = {
            "status": avatar.status_code,
            "sha256": hashlib.sha256(avatar.content).hexdigest()
            if avatar.status_code == 200
            else None,
        }
    for command in reads["commands/files"]["files"]:
        path = f"commands/files/{command['id']}"
        reads[path] = get(path)
    for trace in reads["diagnostics/traces"]["traces"]:
        path = f"diagnostics/traces/{trace['trace_id']}"
        reads[path] = get(path)
    return reads


def _file(directory: Path, read: str) -> Path:
    name = read.replace("/", "__").replace("?", "--").replace("=", "-")
    return directory / f"{name}.json"


def _dump(directory: Path, reads: dict, root: Path) -> None:
    directory.mkdir()
    for read, value in reads.items():
        text = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)
        # Paths of this run's copy, as JSON spells them.
        _file(directory, read).write_text(
            text.replace(json.dumps(str(root))[1:-1], "<root>") + "\n",
            encoding="utf-8",
        )


def test_reads_match_golden(tmp_path):
    source = Path(SOURCE)
    root = tmp_path.resolve()
    workspace, home = root / "workspace", root / "home"
    shutil.copytree(source / "workspace", workspace)
    shutil.copytree(source / "identity", home / ".guildbotics/identity")
    block_runtime(home)
    end = (source / "copied_at").read_text(encoding="utf-8").strip()
    # The copy's providers stay unreached: no credential, and loopback too
    # goes through the harness's dead proxy.
    with serving(root, workspace, home, NO_PROXY="") as stack:
        reads = _read_all(stack, end)
    if RECORD:
        _dump(source / "golden", reads, root)
        return
    actual = root / "actual"
    _dump(actual, reads, root)
    golden = source / "golden"
    names = {path.name for path in golden.iterdir()} | {
        path.name for path in actual.iterdir()
    }
    differing = sorted(
        name
        for name in names
        if not (golden / name).exists()
        or not (actual / name).exists()
        or json.loads((golden / name).read_text(encoding="utf-8"))
        != json.loads((actual / name).read_text(encoding="utf-8"))
    )
    assert not differing, f"{differing} differ from {golden}; responses in {actual}"
