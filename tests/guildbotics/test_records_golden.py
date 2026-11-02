"""The host's files, written by the Python version, as the Rust version
writes and reads them too (``records/golden``)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from guildbotics.drivers.utils import command_boundary
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.correlation import trace_scope
from guildbotics.utils.fileio import get_machine_state_path, get_workspace_root
from guildbotics.utils.workspace_state import (
    read_active_workspace,
    register_workspace,
    write_active_workspace,
)
from guildbotics.workspace.identity import (
    ensure_device_identity,
    ensure_workspace_identity,
)

GOLDEN = Path(__file__).resolve().parents[2] / "records" / "golden"


def _load(name):
    return json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))


DIAGNOSTICS = _load("diagnostics")
FILES = _load("files")


class CommandError(Exception):
    """Named as a command's failure is."""


def _normalized(value, trace_id):
    """Values that differ between runs, as the fixture spells them."""
    if isinstance(value, dict):
        return {
            key: "<timestamp>" if key == "timestamp" else _normalized(item, trace_id)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalized(item, trace_id) for item in value]
    if isinstance(value, str):
        return value.replace(trace_id, "<trace>")
    return value


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("case", DIAGNOSTICS, ids=lambda case: case["id"])
def test_a_run_is_recorded_as_the_fixture_has_it(case):
    trace, run = case["trace"], case["run"]
    ends = run["ends"]
    ended = (
        contextlib.nullcontext() if ends == "finished" else pytest.raises(BaseException)
    )
    with (
        ended,
        trace_scope(
            trace["source"],
            person_id=trace["person_id"],
            command=trace["command"],
            workflow=trace["workflow"],
            attributes=trace["attributes"],
        ) as opened,
        command_boundary(
            command_name=run["command"],
            task_type=trace["source"],
            person_id=run["person"],
        ),
    ):
        if ends == "cancelled":
            raise asyncio.CancelledError
        if ends != "finished":
            raise CommandError(ends["failed"]["error_type"])

    run_dir = get_workspace_root() / ".guildbotics" / "local" / "run"
    index = _jsonl(run_dir / "diagnostics.jsonl")
    transcript = _jsonl(run_dir / "sessions" / f"{opened.trace_id}.jsonl")
    assert _normalized(index, opened.trace_id) == case["index"]
    assert _normalized(transcript, opened.trace_id) == case["transcript"]


@pytest.mark.parametrize("case", DIAGNOSTICS, ids=lambda case: case["id"])
def test_the_fixture_reads_as_its_status(case, tmp_path):
    trace_id = "0123456789abcdef0123456789abcdef"

    def spelled(records):
        text = json.dumps(records).replace("<trace>", trace_id)
        return [
            {**record, "timestamp": f"2026-11-02T00:00:0{n}+00:00"}
            for n, record in enumerate(json.loads(text))
        ]

    run_dir = tmp_path / "run"
    (run_dir / "sessions").mkdir(parents=True)
    for path, records in (
        (run_dir / "diagnostics.jsonl", case["index"]),
        (run_dir / "sessions" / f"{trace_id}.jsonl", case["transcript"]),
    ):
        path.write_text(
            "".join(json.dumps(r) + "\n" for r in spelled(records)), encoding="utf-8"
        )
    store = DiagnosticsStore(run_dir / "diagnostics.jsonl")

    [summary] = store.list_traces()
    assert (summary["trace_id"], summary["status"]) == (trace_id, case["status"])
    assert len(store.get_records(trace_id)) == len(case["transcript"])


def _spelled(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace(json.dumps(value)[1:-1], f"<{name}>")
    return text


def test_the_workspace_files_are_written_as_the_fixture_has_them(tmp_path):
    root = tmp_path / "machine"
    for name in FILES["registry"]["register"]:
        register_workspace(root / name)
    registry = get_machine_state_path("workspaces.json").read_text(encoding="utf-8")
    assert _spelled(registry, {"root": str(root)}) == FILES["registry"]["text"]

    selected = root / "selected"
    selected.mkdir(parents=True)
    write_active_workspace(selected)
    active = get_machine_state_path("active-workspace.json").read_text("utf-8")
    assert _spelled(active, {"root": str(root)}) == FILES["active_workspace"]

    workspace = ensure_workspace_identity(selected)
    text = (selected / ".guildbotics/state/workspace.json").read_text("utf-8")
    expected = FILES["workspace_identity"]
    assert _spelled(text, workspace.model_dump(exclude={"schema_version"})) == expected
    device = ensure_device_identity()
    text = get_machine_state_path("device.json").read_text("utf-8")
    expected = FILES["device_identity"]
    assert _spelled(text, device.model_dump(exclude={"schema_version"})) == expected


def test_the_fixture_files_are_read(tmp_path):
    root = tmp_path / "machine"
    selected = root / "selected"
    selected.mkdir(parents=True)
    active = get_machine_state_path("active-workspace.json")
    active.parent.mkdir(parents=True)
    active.write_text(FILES["active_workspace"].replace("<root>", str(root)), "utf-8")
    assert read_active_workspace().workspace == selected

    values = {
        "workspace_id": "01a0951c-4e6e-7747-a845-11a5ec9ff682",
        "created_at": "2026-11-02T00:00:00.000000Z",
    }
    text = FILES["workspace_identity"]
    for name, value in values.items():
        text = text.replace(f"<{name}>", value)
    (selected / ".guildbotics/state").mkdir(parents=True)
    (selected / ".guildbotics/state/workspace.json").write_text(text, "utf-8")
    assert ensure_workspace_identity(selected).workspace_id == values["workspace_id"]
