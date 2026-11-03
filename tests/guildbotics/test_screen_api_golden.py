"""Python answers for the walking skeleton's screen API.

Regenerate with python -m tests.guildbotics.test_screen_api_golden.
"""

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from guildbotics.app_api.command_files import CommandFileService
from guildbotics.app_api.runtime import AppRuntime
from guildbotics.app_api.traces import list_traces, trace_detail
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader

GOLDEN = Path(__file__).resolve().parents[2] / "records/golden/screens.json"
CONFIG = {
    "team/project.yml": "name: Example\nlanguage: ja\n",
    "team/members/aiko/member.yml": "is_active: true\nprofile: {roles: {}}\n",
    "commands/look.md": "---\nname: {en: Look, ja: 調べる}\ndescription: Inspect {target}\nread_only: true\nargs:\n  target: {default: example}\ninputs: {message: hidden}\n---\nInspect ${1}, {{ other }} and {target}\n",
    "commands/look.fr.md": "French\n",
    "commands/look.en.md": "---\nname: English\n---\nEnglish\n",
    "commands/look.ja.md": "---\nname: 調べる\nread_only: true\n---\n日本語 ${1}\n",
    "commands/nested/args.yaml": "description: 'Inspect ${2}, {place}, {{ other }}, {context} and {child}'\nargs:\n  z: {default: false}\n  a: {required: true}\ncommands: [{path: child}]\ninputs: {message: required, extra_args: optional}\n",
    "commands/code.py": 'COMMAND_METADATA = {"inputs": {"extra_args": "optional"}}\n\ndef main(context, path, count=2, /, flag=True, *, title="x", other=None):\n    """Inspect a file.\n\n    More detail.\n    """\n    return path\n',
    "commands/simple.sh": "#!/bin/sh\necho ok\n",
    "commands/bad.md": "---\nargs: [bad]\ninputs: {message: invalid}\n---\nDraft\n",
    "commands/ignored.metadata.yml": "name: hidden\n",
    "commands/ask.md": "Workspace file shadowed by localized template\n",
    "commands/nested.dir/basic.sh": "#!/bin/sh\necho basic\n",
    "commands/variadic.py": "async def main(ctx, path, *args, title='x', **kwargs):\n    return path\n",
}


IDENTITY = {
    ".guildbotics/config/team/members/aiko/identity.yml": "person_id: aiko\nname: Aiko\nperson_type: agent\n",
}


def write_files(root, files):
    """Place the fixture's files in its own temporary workspace."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def answers(root):
    """Call the Python services, never hand-construct their answers."""
    workspace = root / "workspace"
    config = workspace / ".guildbotics/config"
    write_files(config, CONFIG)
    write_files(root / "identity", IDENTITY)
    with patch.dict(
        os.environ,
        {
            "HOME": str(root),
            "GUILDBOTICS_HOME": str(root / "machine"),
            "GUILDBOTICS_WORKSPACE_ROOT": str(workspace),
            "GUILDBOTICS_CONFIG_DIR": str(config),
            "GUILDBOTICS_IDENTITY_ROOT": str(root / "identity"),
        },
    ):
        service = CommandFileService("ja")
        files = service.list_files().model_dump(mode="json")
        details = [
            service.read_file(item["id"]).model_dump(mode="json")
            for item in files["files"]
        ]
        runtime = AppRuntime.__new__(AppRuntime)
        runtime.workspace_problem = ""
        status = runtime.get_config_status().model_dump(mode="json")
        with patch.object(
            runtime,
            "_get_context",
            return_value=SimpleNamespace(
                team=YamlTeamLoader(str(config / "team")).load()
            ),
        ):
            team = runtime.get_team_summary().model_dump(mode="json")
        # Use the production writer: it routes io to the transcript only and
        # writes counts to the final boundary in the index.
        store = DiagnosticsStore()
        records = []
        for i, (trace, end) in enumerate(
            [
                ("success", "command.finished"),
                ("failure", "command.failed"),
                ("running", None),
            ]
        ):
            for j, (kind, event, payload) in enumerate(
                [
                    ("event", "command.started", {"command": "look", "person": "aiko"}),
                    ("io", "command.output", {"output": "result 日本語\n"}),
                    *(
                        [
                            (
                                "event",
                                end,
                                {"error_type": "CommandError"}
                                if end.endswith("failed")
                                else {},
                            )
                        ]
                        if end
                        else []
                    ),
                ]
            ):
                record = {
                    "kind": kind,
                    "type": event,
                    "trace_id": trace,
                    "timestamp": f"2026-11-03T00:00:{i * 10 + j:02d}+00:00",
                    "person_id": "aiko",
                    "source": "manual",
                    "command": "look",
                    "attributes": {"run.entry": "command", "test": trace},
                    "payload": payload,
                }
                store.record(record)
                records.append(record)
        result = {
            "files": files,
            "team": team,
            "details": details,
            "status": status,
            "index": [
                json.loads(line)
                for line in (workspace / ".guildbotics/local/run/diagnostics.jsonl")
                .read_text()
                .splitlines()
            ],
            "sessions": {
                p.name: p.read_text()
                for p in (workspace / ".guildbotics/local/run/sessions").glob("*.jsonl")
            },
            "traces": list_traces(store).model_dump(mode="json"),
            "trace_details": [
                trace_detail(store, name).model_dump(mode="json")
                for name in ["success", "failure", "running", "missing"]
            ],
        }
        return json.loads(
            json.dumps(result, ensure_ascii=False).replace(str(root), "<root>")
        )


def test_python_screen_answers_match_golden(tmp_path):
    assert (
        answers(tmp_path) == json.loads(GOLDEN.read_text(encoding="utf-8"))["expected"]
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory:
        expected = answers(Path(directory).resolve())
    GOLDEN.write_text(
        json.dumps(
            {"config": CONFIG, "identity": IDENTITY, "expected": expected},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
