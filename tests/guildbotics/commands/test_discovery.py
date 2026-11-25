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


def test_unsuffixed_workspace_overrides_unsuffixed_template(
    command_env: SimpleNamespace,
) -> None:
    shared = _shared(command_env, "review.md")
    _template(command_env, "review.md")

    resolved = discovery.resolve_command_path("review", "ja")

    assert resolved == shared


def test_resolution_prefers_member_override(
    command_env: SimpleNamespace,
) -> None:
    _shared(command_env, "greet.md")
    member = _member(command_env, "bot", "greet.md")
    assert discovery.resolve_command_path("greet", "en", person_id="bot") == member


def test_locale_inner_prefers_language_over_english(
    command_env: SimpleNamespace,
) -> None:
    _shared(command_env, "hello.en.md")
    localized = _shared(command_env, "hello.ja.md")

    assert discovery.resolve_command_path("hello", "ja") == localized


@pytest.mark.parametrize(
    ("filename", "language"),
    [
        ("report.metadata.yml", "metadata"),
        ("report.metadata.en.yml", "en"),
        ("report.metadata.ja.yaml", "ja"),
    ],
)
def test_reserved_metadata_files_are_not_command_candidates(
    command_env: SimpleNamespace, filename: str, language: str
) -> None:
    _shared(command_env, filename, "name: Reserved metadata\n")

    result = discovery.iter_command_candidate_names(
        [command_env.config / "commands"], language
    )

    assert result == []


@pytest.mark.parametrize("language", ["en", "ja"])
def test_ask_resolves_from_bundled_template(tmp_path, monkeypatch, language):
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(tmp_path))

    path = discovery.resolve_command_path("ask", language, person_id="alice")

    assert path == fileio.get_template_path() / "commands" / f"ask.{language}.md"
    metadata = fileio.load_markdown_with_frontmatter(path)
    assert metadata["brain"] == "agent"
    assert metadata["inputs"] == {"message": "required"}
