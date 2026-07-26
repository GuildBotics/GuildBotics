"""Unit tests for shared command discovery and resolution precedence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.commands import discovery
from guildbotics.utils import fileio


@pytest.fixture
def command_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolate config + template roots so resolution is hermetic."""
    config_dir = tmp_path / "config"
    template_dir = tmp_path / "templates"
    config_dir.mkdir()
    template_dir.mkdir()
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(fileio, "get_template_path", lambda: template_dir)
    monkeypatch.setattr(discovery, "get_template_path", lambda: template_dir)
    return SimpleNamespace(config=config_dir, template=template_dir)


def _write(path: Path, content: str = "body") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _shared(env: SimpleNamespace, name: str, content: str = "body") -> Path:
    return _write(env.config / "commands" / name, content)


def _member(
    env: SimpleNamespace, person_id: str, name: str, content: str = "body"
) -> Path:
    return _write(
        env.config / "team" / "members" / person_id / "commands" / name, content
    )


def _template(env: SimpleNamespace, name: str, content: str = "body") -> Path:
    return _write(env.template / "commands" / name, content)


def _commands(pairs: list[tuple[str, Path]]) -> dict[str, Path]:
    return {command: path for command, path in pairs}


def test_shared_list_only_scans_shared_root(command_env: SimpleNamespace) -> None:
    shared = _shared(command_env, "review.md")
    _member(command_env, "bot", "member_only.md")

    result = _commands(list(discovery.iter_effective_shared_commands("en")))

    assert result == {"review": shared}


def test_shared_list_returns_nested_command_names(
    command_env: SimpleNamespace,
) -> None:
    nested = _shared(command_env, "workflows/review.yaml", "commands: []\n")

    result = _commands(list(discovery.iter_effective_shared_commands("en")))

    assert result == {"workflows/review": nested}


def test_extension_outer_prefers_md_over_localized_py(
    command_env: SimpleNamespace,
) -> None:
    # A plain .md must win over a localized .py because extension is the outer
    # sort key in the registry order (.md before .py).
    md = _shared(command_env, "task.md")
    _shared(command_env, "task.ja.py", "def main():\n    return ''\n")

    assert discovery.resolve_command_path("task", "ja") == md


def test_localized_template_shadows_workspace_unsuffixed_file(
    command_env: SimpleNamespace,
) -> None:
    _shared(command_env, "review.md")
    localized_template = _template(command_env, "review.ja.md")

    resolved = discovery.resolve_command_path("review", "ja")

    assert resolved == localized_template
    # And because it resolves to a template, it is not editor-eligible.
    result = _commands(list(discovery.iter_effective_shared_commands("ja")))
    assert "review" not in result


def test_unsuffixed_workspace_overrides_unsuffixed_template(
    command_env: SimpleNamespace,
) -> None:
    shared = _shared(command_env, "review.md")
    _template(command_env, "review.md")

    resolved = discovery.resolve_command_path("review", "ja")

    assert resolved == shared
    result = _commands(list(discovery.iter_effective_shared_commands("ja")))
    assert result == {"review": shared}


def test_editor_discovery_matches_runtime_resolver(
    command_env: SimpleNamespace,
) -> None:
    shared = _shared(command_env, "workflows/review.md")

    listed = _commands(list(discovery.iter_effective_shared_commands("en")))
    resolved = discovery.resolve_command_path("workflows/review", "en")

    assert listed["workflows/review"] == shared == resolved


def test_effective_commands_include_member_override(
    command_env: SimpleNamespace,
) -> None:
    _shared(command_env, "greet.md")
    member = _member(command_env, "bot", "greet.md")
    roots = [
        command_env.config / "team" / "members" / "bot" / "commands",
        discovery.get_shared_commands_root(),
    ]

    result = _commands(
        list(discovery.iter_effective_commands(roots, "en", person_id="bot"))
    )

    assert result["greet"] == member


def test_command_source_classifies_template_and_workspace(
    command_env: SimpleNamespace,
) -> None:
    shared = _shared(command_env, "a.md")
    template = _template(command_env, "b.md")

    assert discovery.command_source(shared) == "workspace"
    assert discovery.command_source(template) == "template"


def test_locale_inner_prefers_language_over_english(
    command_env: SimpleNamespace,
) -> None:
    _shared(command_env, "hello.en.md")
    localized = _shared(command_env, "hello.ja.md")

    assert discovery.resolve_command_path("hello", "ja") == localized


def test_non_matching_locale_is_not_listed(command_env: SimpleNamespace) -> None:
    _shared(command_env, "only.fr.md")

    result = _commands(list(discovery.iter_effective_shared_commands("ja")))

    assert result == {}


@pytest.mark.parametrize(
    ("filename", "language"),
    [
        ("report.metadata.yml", "metadata"),
        ("report.metadata.en.yml", "en"),
        ("report.metadata.ja.yaml", "ja"),
    ],
)
def test_legacy_metadata_files_are_not_command_candidates(
    command_env: SimpleNamespace, filename: str, language: str
) -> None:
    _shared(command_env, filename, "name: Legacy metadata\n")

    result = discovery.iter_command_candidate_names(
        [discovery.get_shared_commands_root()], language
    )

    assert result == []
