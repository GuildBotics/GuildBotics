"""Which file a command name runs, what it declares, and the team it runs
in, as the Python version decides them and the Rust version too
(``records/golden/commands.json``).

The fixture's inputs are written by hand and its answers by this module:
``uv run --no-sync python -m tests.guildbotics.test_commands_golden`` writes
them again, after a bundled command or the Python version changed.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from guildbotics.commands.discovery import is_command_source_path, resolve_command_path
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import (
    command_access,
    parse_python_metadata_from_module,
)
from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
from guildbotics.utils.fileio import (
    GUILDBOTICS_CONFIG_DIR,
    GUILDBOTICS_IDENTITY_ROOT,
    get_template_path,
)

GOLDEN = Path(__file__).resolve().parents[2] / "records" / "golden" / "commands.json"
FIXTURE = json.loads(GOLDEN.read_text(encoding="utf-8"))
LANGUAGES = ("ja", "en")


def _relative(path: Path | None, roots: dict[str, Path]) -> str | None:
    if path is None:
        return None
    for name, root in roots.items():
        if path.is_relative_to(root):
            return f"{name}/{path.relative_to(root).as_posix()}"
    raise AssertionError(path)


def _access(path: Path) -> dict | None:
    """What the command declares, or None when the declaration is refused."""
    try:
        declared = command_access(path)
    except CommandError:
        return None
    return {"read_only": declared.read_only, "inspects": sorted(declared.inspects)}


def _python_metadata(source: str) -> dict | None:
    """The module's declaration, or None when it is refused."""
    try:
        found = parse_python_metadata_from_module(ast.parse(source))
    except CommandError:
        return None
    return json.loads(json.dumps(found, default=str))


def bundled() -> dict:
    """What the bundled commands resolve to and declare."""
    commands = get_template_path() / "commands"
    files = sorted(
        p for p in commands.rglob("*") if p.is_file() and is_command_source_path(p)
    )
    names = sorted(
        {
            p.relative_to(commands).with_name(p.name.split(".")[0]).as_posix()
            for p in files
        }
    )
    roots = {"templates": get_template_path()}
    resolved = {
        f"{name}@{language}": _relative(
            resolve_command_path(name, language, None), roots
        )
        for name in names
        for language in LANGUAGES
    }
    return {
        "resolved": resolved,
        "access": {_relative(p, roots): _access(p) for p in files},
        "python_metadata": {
            _relative(p, roots): _python_metadata(p.read_text(encoding="utf-8"))
            for p in files
            if p.suffix == ".py"
        },
    }


def python_source(case: dict, root: Path) -> dict:
    """What a module declares, as the Python version reads it."""
    metadata = _python_metadata(case["source"])
    path = root / "command.py"
    path.write_text(case["source"], encoding="utf-8", newline="")
    return {
        "metadata": metadata,
        "access": _access(path) if metadata is not None else None,
    }


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def workspace(case: dict, root: Path, setenv: Callable[[str, str], None]) -> dict:
    """What a workspace's own commands and team resolve to."""
    config, identity = root / "config", root / "identity"
    _write(config, case["config"])
    _write(identity / ".guildbotics/config", case["identity"])
    setenv(GUILDBOTICS_CONFIG_DIR, str(config))
    setenv(GUILDBOTICS_IDENTITY_ROOT, str(identity))
    roots = {"config": config, "templates": get_template_path()}
    resolved = {}
    for name, language, person in case["resolve"]:
        key = f"{name}@{language}@{person}"
        try:
            resolved[key] = _relative(
                resolve_command_path(name, language, person), roots
            )
        except ValueError:
            resolved[key] = "invalid person"
    access = {
        name: _access(config / name)
        for name in sorted(case["config"])
        if name.startswith("commands/") and is_command_source_path(config / name)
    }
    try:
        team = YamlTeamLoader(str(config / "team")).load()
    except ValueError:
        loaded = None
    else:
        loaded = {
            "language": team.project.get_language_code(),
            "default_person_id": team.get_default_person_id(),
            "members": [
                {
                    "person_id": m.person_id,
                    "name": m.name,
                    "person_type": m.person_type,
                    "is_active": m.is_active,
                }
                for m in team.members
            ],
        }
    return {"resolved": resolved, "access": access, "team": loaded}


def test_the_bundled_commands_are_as_the_fixture_has_them():
    assert bundled() == FIXTURE["bundled"]


@pytest.mark.parametrize("case", FIXTURE["python_sources"], ids=lambda c: c["id"])
def test_a_module_declares_as_the_fixture_has_it(case, tmp_path):
    assert python_source(case, tmp_path) == case["expected"]


@pytest.mark.parametrize("case", FIXTURE["workspaces"], ids=lambda c: c["id"])
def test_a_workspace_is_read_as_the_fixture_has_it(case, tmp_path, monkeypatch):
    assert workspace(case, tmp_path, monkeypatch.setenv) == case["expected"]


def main() -> None:
    """Write the fixture's answers again from its inputs."""
    fixture = dict(FIXTURE)
    saved = dict(os.environ)
    try:
        fixture["bundled"] = bundled()
        for case in fixture["python_sources"]:
            case["expected"] = python_source(case, Path(tempfile.mkdtemp()))
        for case in fixture["workspaces"]:
            case["expected"] = workspace(
                case, Path(tempfile.mkdtemp()), os.environ.__setitem__
            )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    GOLDEN.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
