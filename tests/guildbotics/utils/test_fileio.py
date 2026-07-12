from pathlib import Path

import pytest

from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    _clean_data,
    find_package_subdir,
    get_config_path,
    get_machine_state_path,
    get_machine_state_root,
    get_primary_config_path,
    get_storage_path,
    get_workspace_data_root,
    load_markdown_with_frontmatter,
    load_yaml_file,
    resolve_workspace_data_root,
    save_yaml_file,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_load_markdown_with_frontmatter_handles_newlines(tmp_path, newline):
    """Front matter parses correctly when files use LF or CRLF newlines."""
    content = ("---\nbrain: agent\n---\nBody text\n").replace("\n", newline)

    path = tmp_path / "prompt.md"
    path.write_text(content, encoding="utf-8")

    metadata = load_markdown_with_frontmatter(path)
    assert metadata["brain"] == "agent"
    assert metadata["body"] == "Body text"


def test_find_package_subdir_templates_exists():
    """find_package_subdir returns an existing 'templates' directory from package root."""
    p = find_package_subdir(Path("templates"))
    assert p.name == "templates"
    assert p.exists() and p.is_dir()


def test_get_config_path_prefers_env_over_template(tmp_path, monkeypatch):
    """When env config contains the file, it takes precedence over templates."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()

    env_file = env_dir / "foo.yaml"
    env_file.write_text("a: 1\n", encoding="utf-8")

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    resolved = get_config_path("foo.yaml")
    assert resolved == env_file
    assert load_yaml_file(resolved) == {"a": 1}


def test_get_config_path_uses_template_when_env_missing_file(tmp_path, monkeypatch):
    """If the workspace config lacks the file, falls back to package templates."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()

    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    resolved = get_config_path("team/defaults.yml")
    assert "templates" in resolved.parts
    assert resolved.name == "defaults.yml"


def test_get_config_path_language_specific_and_fallback(tmp_path, monkeypatch):
    """Language-specific file resolves first; otherwise falls back to '.en'."""
    env_dir = tmp_path / "envcfg"
    env_dir.mkdir()
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(env_dir))

    ja_file = env_dir / "prompt.ja.yaml"
    en_file = env_dir / "prompt.en.yaml"
    ja_file.write_text("msg: ja\n", encoding="utf-8")
    en_file.write_text("msg: en\n", encoding="utf-8")

    # Prefers language-specific
    resolved_ja = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_ja == ja_file

    # Remove ja to force fallback to en
    ja_file.unlink()
    resolved_fallback = get_config_path("prompt.yaml", language_code="ja")
    assert resolved_fallback == en_file


def test_get_primary_config_path_uses_absolute_workspace_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GUILDBOTICS_CONFIG_DIR", raising=False)

    assert get_primary_config_path(Path("team/project.yml")) == (
        tmp_path / ".guildbotics/config/team/project.yml"
    )


def test_get_storage_path_prefers_data_dir_env_when_home_changes(tmp_path, monkeypatch):
    data_dir = tmp_path / "stable-data"
    changed_home = tmp_path / "agent-home"
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("HOME", str(changed_home))

    assert get_storage_path() == data_dir


def test_get_storage_path_resolves_relative_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GUILDBOTICS_DATA_DIR", "stable-data")

    assert get_storage_path() == tmp_path / "stable-data"


def test_get_machine_state_root_ignores_data_dir_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data_dir = tmp_path / "workspace-data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(GUILDBOTICS_DATA_DIR, str(data_dir))

    assert get_machine_state_root() == home / ".guildbotics" / "data"
    assert get_machine_state_path("run", "service.lock") == (
        home / ".guildbotics" / "data" / "run" / "service.lock"
    )


def test_resolve_workspace_data_root_prefers_workspace_env_over_inherited(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = workspace / ".env"
    env_file.write_text(
        "GUILDBOTICS_DATA_DIR=workspace-data\nOTHER=value\n",
        encoding="utf-8",
    )

    assert resolve_workspace_data_root(
        workspace,
        env_file,
        inherited_data_dir=str(tmp_path / "inherited-data"),
    ) == (tmp_path / "workspace-data").resolve(strict=False)


def test_resolve_workspace_data_root_uses_inherited_without_workspace_override(
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = workspace / ".env"
    env_file.write_text("OTHER=value\n", encoding="utf-8")

    assert resolve_workspace_data_root(
        workspace,
        env_file,
        inherited_data_dir=str(tmp_path / "inherited-data"),
    ) == (tmp_path / "inherited-data").resolve(strict=False)


def test_resolve_workspace_data_root_defaults_to_workspace_data(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert resolve_workspace_data_root(workspace) == (
        workspace / ".guildbotics" / "data"
    ).resolve(strict=False)


def test_get_workspace_data_root_defaults_to_workspace_root(tmp_path, monkeypatch):
    monkeypatch.delenv(GUILDBOTICS_DATA_DIR, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert get_workspace_data_root(workspace) == (
        workspace / ".guildbotics" / "data"
    ).resolve(strict=False)


def test_clean_data_removes_none_and_empty_keys():
    """_clean_data drops None/'' keys in dicts, preserves list items."""
    raw = {
        "a": 1,
        "b": None,
        "c": "",
        "d": {"e": None, "f": "", "g": 2},
        "h": [
            {"i": None, "j": ""},
            5,
            None,
            "",
        ],
    }
    cleaned = _clean_data(raw)
    assert cleaned == {
        "a": 1,
        "d": {"g": 2},
        "h": [{}, 5, None, ""],
    }


def test_save_yaml_file_roundtrip_cleans(tmp_path):
    """save_yaml_file writes cleaned YAML; loading reproduces cleaned structure."""
    raw = {
        "title": "example",
        "unused": None,
        "nested": {"x": 1, "drop": ""},
        "items": [
            {"keep": 1, "omit": None},
            {"omit": ""},
            None,
            "",
        ],
    }
    expected = {
        "title": "example",
        "nested": {"x": 1},
        "items": [
            {"keep": 1},
            {},
            None,
            "",
        ],
    }

    out = tmp_path / "out.yaml"
    save_yaml_file(out, raw)
    loaded = load_yaml_file(out)
    assert loaded == expected
