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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.blackbox.golden import copy
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
    # The server's OS, including person-scoped configuration reads.
    "config/intelligences": ("platform",),
}


def _comparable(read, value):
    # File comparisons use the same endpoint identity as live reads.
    if read.endswith(".json"):
        read = read[:-5].replace("__", "/").replace("--", "?")
    path = read.split("?", 1)[0]
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in VOLATILE.get(path, ())}
    if path in ("commands/options", "commands/routine-options"):
        value = dict(value)
        value["options"] = [
            dict(
                option,
                requirements=[_requirement(item) for item in option["requirements"]],
            )
            for option in value["options"]
        ]
    return value


def _requirement(item):
    # The harness blocks runtime placement. Keep the localized text,
    # destination, kind and satisfied flag while allowing OS error text to vary.
    marker = "配置できませんでした: "
    message = item.get("message", "")
    if item.get("kind") == "environment" and marker in message:
        prefix, suffix = message.split(marker, 1)
        assert suffix.strip(), "Runtime placement failure must include its OS cause"
        return dict(item, message=prefix + marker + "<os-error>")
    return item


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
        # The workspace config the member was read in, which its save names,
        # is answered since #133; the golden of the frozen server lacks it.
        member_read = get(f"config/members/{person}")
        member_read.pop("config_dir", None)
        reads[f"config/members/{person}"] = member_read
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
    paths = [
        f"diagnostics/traces/{trace['trace_id']}"
        for trace in reads["diagnostics/traces"]["traces"]
    ]
    with ThreadPoolExecutor(max_workers=4) as workers:
        reads.update(zip(paths, workers.map(get, paths), strict=True))
    # A bundled command's path is where the server is installed.
    for read, value in reads.items():
        if read.startswith(("commands/options", "commands/routine-options")):
            for option in value["options"]:
                if option["source"] == "template":
                    del option["path"]
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


@pytest.mark.skipif(
    not SOURCE, reason="GUILDBOTICS_BLACKBOX_GOLDEN names no production copy"
)
@pytest.mark.timeout(1200)
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
        or _comparable(name, json.loads((golden / name).read_text(encoding="utf-8")))
        != _comparable(name, json.loads((actual / name).read_text(encoding="utf-8")))
    )
    assert not differing, f"{differing} differ from {golden}; responses in {actual}"


def test_copy_leaves_production_behind(tmp_path, symlinks):
    """Synthetic production: the copy names no keychain namespace or service of it."""
    production = tmp_path / "production"
    outside = production / "outside.yml"
    outside.parent.mkdir()
    outside.write_text("shared: true\n", encoding="utf-8")
    roots = {"workspace": production / "gb", "identity": production / "identity"}
    for root in roots.values():
        state = root / ".guildbotics"
        for part in ("config", "local/run"):
            (state / part).mkdir(parents=True)
        (state / "local/secrets.json").write_text('{"store_id": "prod"}')
        (state / "config/secrets.yml").write_text("keys:\n- A_TOKEN\n")
        (state / "config/link.yml").symlink_to(outside)
    workspace = roots["workspace"] / ".guildbotics"
    (workspace / "state").mkdir()
    for excluded in ("clones/x", "member_git/x", "agent_environment/x", "work/x"):
        (workspace / "local" / excluded).mkdir(parents=True)
    (workspace / "data/workspaces").mkdir(parents=True)
    (workspace / "local/run/command-inputs").mkdir()
    (workspace / "local/run/diagnostics.jsonl").write_text("{}\n")
    (workspace / "local/service.yml").write_text("start_with_server: true\n")
    (workspace / "config/transcripts.yml").write_text("retention_days: 30\n")

    destination = tmp_path / "copy"
    copy(roots["workspace"], roots["identity"], destination)

    for name in roots:
        copied = destination / name / ".guildbotics"
        assert not (copied / "local/secrets.json").exists()
        assert (copied / "config/secrets.yml").read_text() == "keys:\n- A_TOKEN\n"
        link = copied / "config/link.yml"
        assert not link.is_symlink() and link.read_text() == "shared: true\n"
    copied = destination / "workspace/.guildbotics"
    assert sorted(path.name for path in copied.iterdir()) == [
        "config",
        "local",
        "state",
    ]
    assert sorted(path.name for path in (copied / "local").iterdir()) == [
        "run",
        "service.yml",
    ]
    assert [path.name for path in (copied / "local/run").iterdir()] == [
        "diagnostics.jsonl"
    ]
    assert "start_with_server: false" in (copied / "local/service.yml").read_text()
    assert "retention_days: 36500" in (copied / "config/transcripts.yml").read_text()


def test_comparison_excludes_only_machine_fields():
    assert _comparable(
        "config/intelligences?person_id=person",
        {"platform": "darwin", "models": [2, 1], "x": 3},
    ) == {"models": [2, 1], "x": 3}
    assert _comparable("activity/history", {"timestamp": "exact", "links": [2, 1]}) == {
        "timestamp": "exact",
        "links": [2, 1],
    }


def test_os_error_normalization_preserves_all_other_requirement_fields():
    base = {
        "kind": "environment",
        "satisfied": False,
        "message": "ランタイムを /one に配置できませんでした: Python OS error",
    }
    same = dict(base, message="ランタイムを /one に配置できませんでした: Rust OS error")
    assert _requirement(base) == _requirement(same)
    for changed in (
        dict(same, satisfied=True),
        dict(same, kind="cli_agent"),
        dict(same, message=same["message"].replace("/one", "/two")),
    ):
        assert _requirement(base) != _requirement(changed)
    with pytest.raises(AssertionError, match="OS cause"):
        _requirement(dict(base, message="ランタイムを /one に配置できませんでした: "))
